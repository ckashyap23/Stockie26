from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.export import signal_to_csv
from app.jobs import JobBusy, JobManager, default_runner
from app.pipeline.ingest_job import run_ingest
from app.pipeline.seeds import load_seeds, load_sources
from app.report import INCLUDE_MODES, filter_report
from app.security import require_api_key
from app.storage.file_store import FileSignalStore

IST = ZoneInfo("Asia/Kolkata")

public = APIRouter()
v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


class RunRequest(BaseModel):
    target_date: date | None = Field(default=None, description="Trading date to forecast; default today (IST)")
    wait: bool = Field(default=False, description="false (default): start a background run and return 202 + job; true: block and return the result")
    persist: bool = True
    replay: bool = Field(default=False, description="rebuild from the archive exactly as it stood at the cutoff (point-in-time), no live fetch")
    cutoff_time: str | None = Field(default=None, description="HH:MM IST override of the window end on the target date (default WINDOW_END_TIME)")


def _store(settings: Settings) -> FileSignalStore:
    return FileSignalStore(settings.data_dir)


def job_manager(app, settings: Settings) -> JobManager:
    """One JobManager per data dir (the single-run guard must be shared by every request and by the scheduler)."""
    managers = getattr(app.state, "job_managers", None)
    if managers is None:
        managers = app.state.job_managers = {}
    key = str(settings.data_dir)
    if key not in managers:
        managers[key] = JobManager(settings.data_dir, getattr(app.state, "job_runner", default_runner))
    return managers[key]


def get_jobs(request: Request, settings: Settings = Depends(get_settings)) -> JobManager:
    return job_manager(request.app, settings)


def _parse_cutoff(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "cutoff_time must be HH:MM")


def _check_include(include: str) -> str:
    if include not in INCLUDE_MODES:
        raise HTTPException(422, f"include must be one of {sorted(INCLUDE_MODES)}")
    return include


@public.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok", "service": "news-signal"}


@v1.post("/ingest", tags=["ops"])
def ingest(settings: Settings = Depends(get_settings)) -> dict:
    """Fetch the feeds into the archive (seconds, no LLM). Schedule this every ~30 minutes so the 15:30-08:30 window is complete."""
    return run_ingest(settings)


@v1.post("/signal/run", tags=["signal"], status_code=200)
def run(request: RunRequest, response: Response, settings: Settings = Depends(get_settings), jobs: JobManager = Depends(get_jobs)) -> dict:
    """Start a run. Default: background job, returns 202 with a job to poll at GET /v1/runs/{job_id}. With wait=true: block and return the signal."""
    target = request.target_date or datetime.now(IST).date()
    cutoff = _parse_cutoff(request.cutoff_time)
    try:
        if request.wait:
            job, result = jobs.run_now(settings, target, request.replay, cutoff, request.persist)
            if job["status"] != "succeeded":
                raise HTTPException(500, job.get("error") or "run failed")
            return result or {}
        job = jobs.submit(settings, target, request.replay, cutoff, request.persist)
    except JobBusy as busy:
        raise HTTPException(409, detail={"message": "a run is already in progress", "job": busy.job})
    response.status_code = 202
    return job


@v1.get("/runs", tags=["signal"])
def list_runs(limit: int = Query(default=20, ge=1, le=200), jobs: JobManager = Depends(get_jobs)) -> list[dict]:
    return jobs.list(limit)


@v1.get("/runs/{job_id}", tags=["signal"])
def get_run(job_id: str, jobs: JobManager = Depends(get_jobs)) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No such run.")
    return job


@v1.get("/signal", tags=["signal"])
def get_signal(
    target_date: date | None = Query(default=None, description="YYYY-MM-DD; omit for the latest stored signal"),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Read a stored signal exactly as saved (all judged items). Prefer GET /v1/signal/{date} for a filtered view."""
    store = _store(settings)
    payload = store.load(target_date) if target_date else store.latest()
    if payload is None:
        raise HTTPException(404, "No stored signal. Run POST /v1/signal/run first.")
    return payload


@v1.get("/signal/{target_date}", tags=["signal"])
def get_signal_for_date(
    target_date: date,
    include: str = Query(default="relevant", description="scored | relevant | all: which judged items to list under each seed"),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Final score plus the per-seed array: every seed with its score and its deduplicated news events."""
    payload = _store(settings).load(target_date)
    if payload is None:
        raise HTTPException(404, f"No stored signal for {target_date}. Run POST /v1/signal/run first.")
    return {**payload, "seeds": filter_report(payload.get("seeds") or [], _check_include(include))}


@v1.get("/signal/{target_date}/csv", tags=["signal"])
def download_signal_csv(
    target_date: date,
    view: str = Query(default="articles", description="articles: one row per seed x news event | seeds: one row per seed"),
    include: str = Query(default="relevant", description="scored | relevant | all"),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Downloadable CSV (UTF-8 with BOM, opens in Excel). The final score is repeated on every row."""
    payload = _store(settings).load(target_date)
    if payload is None:
        raise HTTPException(404, f"No stored signal for {target_date}. Run POST /v1/signal/run first.")
    if view not in {"articles", "seeds"}:
        raise HTTPException(422, "view must be 'articles' or 'seeds'")
    suffix = "" if view == "articles" else "_seeds"
    return Response(
        content=signal_to_csv(payload, view, _check_include(include)), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="news_signal_{target_date}{suffix}.csv"'},
    )


@v1.get("/archive/stats", tags=["signal"])
def archive_stats(settings: Settings = Depends(get_settings)) -> dict:
    """What the timestamped article archive holds (needed to backtest / calibrate)."""
    from app.calibration.model import Calibration
    from app.storage.archive import ArticleArchive

    calibration = Calibration.load(settings.calibration_file())
    return {
        "archive": ArticleArchive(settings.archive_file()).stats(),
        "calibration": asdict(calibration) if calibration else None,
    }


@v1.get("/events", tags=["catalogue"])
def list_events(family: str | None = None, settings: Settings = Depends(get_settings)) -> list[dict]:
    seeds = load_seeds(settings.seeds_path)
    return [asdict(s) for s in seeds if family is None or s.family == family]


@v1.get("/sources", tags=["catalogue"])
def list_sources(settings: Settings = Depends(get_settings)) -> list[dict]:
    return [asdict(s) for s in load_sources(settings.feeds_path)]
