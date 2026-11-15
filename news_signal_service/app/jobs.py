"""Asynchronous signal runs.

A run takes minutes (dedupe, ~25 LLM calls), longer than a web request should be held open, so POST /v1/signal/run starts a job and
returns its id; the caller polls GET /v1/runs/{id}. Rules:
  - ONE run at a time (a second request gets 409 with the running job): overlapping runs would double-spend LLM quota and fight over files.
  - Job status lives in DATA_DIR/jobs/<id>.json, so it survives restarts; a job found 'queued'/'running' at startup is marked failed
    ("interrupted by a restart") instead of hanging forever.
  - The service must run as ONE process (uvicorn --workers 1): the single-run guard is in-process.
"""
from __future__ import annotations

import json
import logging
import threading
import time as _time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from app.config import Settings
from app.storage.file_store import FileSignalStore

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

Runner = Callable[[Settings, date, bool, "time | None", bool], dict]


class JobBusy(Exception):
    def __init__(self, job: dict) -> None:
        super().__init__(f"run {job.get('job_id')} is still in progress")
        self.job = job


def default_runner(settings: Settings, target: date, replay: bool, cutoff_time: "time | None", persist: bool) -> dict:
    """Run the pipeline; when persisting, record every stage's artifacts and store the result as the date's latest signal."""
    from app.pipeline.orchestrator import run_signal

    cutoff = datetime.combine(target, cutoff_time, tzinfo=IST) if cutoff_time else None
    result = run_signal(target, settings, replay=replay, record=persist, cutoff=cutoff).to_dict()
    if persist:
        FileSignalStore(settings.data_dir).save(target, result)
        if not replay:
            result["published"] = _publish(settings, result)
    return result


def _publish(settings: Settings, result: dict) -> dict:
    """Write the score to NiftyPrediction.news_score; never fail the run because the publish failed."""
    from app.publish.prediction_table import connection_string, publish_signal

    if not connection_string(settings):
        return {"written": False, "reason": "not configured"}
    try:
        return publish_signal(settings, result)
    except Exception as exc:  # noqa: BLE001 - the signal is already saved; the publish can be retried with `python -m app.publish`
        log.warning("news score publish failed: %s", exc)
        return {"written": False, "reason": f"publish failed: {exc}"}


class JobManager:
    def __init__(self, data_dir: Path, runner: Runner = default_runner) -> None:
        self._dir = data_dir / "jobs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._runner = runner
        self._lock = threading.Lock()
        self._active: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="signal-run")
        self._mark_interrupted()

    # -- storage -------------------------------------------------------------------------------------------------------
    def _path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.json"

    def _write(self, job: dict) -> None:
        tmp = self._path(job["job_id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
        for attempt in range(20):  # Windows refuses to replace a file another thread is reading at that instant
            try:
                tmp.replace(self._path(job["job_id"]))
                return
            except PermissionError:
                if attempt == 19:
                    raise
                _time.sleep(0.05)

    def get(self, job_id: str) -> dict | None:
        path = self._path(job_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def list(self, limit: int = 20) -> list[dict]:
        jobs = [json.loads(p.read_text(encoding="utf-8")) for p in self._dir.glob("*.json")]
        return sorted(jobs, key=lambda j: j["requested_at"], reverse=True)[:limit]

    def _mark_interrupted(self) -> None:
        for job in self.list(limit=10_000):
            if job["status"] in {"queued", "running"}:
                job.update(status="failed", finished_at=datetime.now(IST).isoformat(),
                           error="interrupted: the service restarted before this run finished")
                self._write(job)

    # -- running -------------------------------------------------------------------------------------------------------
    def _claim(self, target: date, replay: bool, cutoff_time: "time | None") -> dict:
        with self._lock:
            if self._active:
                raise JobBusy(self.get(self._active) or {"job_id": self._active, "status": "running"})
            now = datetime.now(IST)
            job = {
                "job_id": f"{target.isoformat()}-{now:%H%M%S}-{uuid.uuid4().hex[:4]}", "status": "queued", "target_date": target.isoformat(),
                "replay": replay, "cutoff_time": cutoff_time.isoformat(timespec="minutes") if cutoff_time else None,
                "requested_at": now.isoformat(), "started_at": None, "finished_at": None, "error": None,
            }
            self._active = job["job_id"]
            self._write(job)
            return job

    def _execute(self, job: dict, settings: Settings, target: date, replay: bool, cutoff_time: "time | None", persist: bool) -> dict | None:
        job.update(status="running", started_at=datetime.now(IST).isoformat())
        self._write(job)
        result = None
        try:
            result = self._runner(settings, target, replay, cutoff_time, persist)
            job.update(
                status="succeeded", run_id=result.get("run_id", ""), score=result.get("score"), direction=result.get("direction"),
                confidence=result.get("confidence"), warnings=len(result.get("warnings", [])),
                result_url=f"/v1/signal/{target.isoformat()}", csv_url=f"/v1/signal/{target.isoformat()}/csv",
            )
        except Exception as exc:  # noqa: BLE001 - a failed run is reported on the job, never crashes the service
            log.exception("signal run %s failed", job["job_id"])
            job.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            job["finished_at"] = datetime.now(IST).isoformat()
            self._write(job)
            with self._lock:
                self._active = None
        return result

    def submit(self, settings: Settings, target: date, replay: bool = False, cutoff_time: "time | None" = None, persist: bool = True) -> dict:
        """Start a background run; returns the queued job immediately. Raises JobBusy if a run is in progress."""
        job = self._claim(target, replay, cutoff_time)
        self._executor.submit(self._execute, job, settings, target, replay, cutoff_time, persist)
        return dict(job)

    def run_now(self, settings: Settings, target: date, replay: bool = False, cutoff_time: "time | None" = None,
                persist: bool = True) -> tuple[dict, dict | None]:
        """Run inline (still under the single-run guard). Returns (job, result)."""
        job = self._claim(target, replay, cutoff_time)
        result = self._execute(job, settings, target, replay, cutoff_time, persist)
        return dict(job), result
