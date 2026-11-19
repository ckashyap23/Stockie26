"""Hosting layer: window, ingest, archive-backed live runs, per-seed report, CSV, async jobs, API, cron trigger."""
import csv
import io
import json
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import trigger
from app.config import SERVICE_ROOT, Settings, get_settings
from app.domain import Article, ImpactJudgement, RelevantArticle
from app.export import signal_to_csv
from app.jobs import JobBusy, JobManager
from app.main import create_app
from app.pipeline import orchestrator
from app.pipeline.dedupe import EventCluster
from app.pipeline.ingest_job import run_ingest
from app.pipeline.orchestrator import news_window
from app.pipeline.seeds import load_seeds
from app.report import build_seed_report, filter_report
from app.storage.archive import ArticleArchive
from app.storage.file_store import FileSignalStore

IST = ZoneInfo("Asia/Kolkata")
SEEDS = load_seeds(SERVICE_ROOT / "data" / "event_seeds.csv")


def _dt(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def _art(i, title, source="Reuters", published=None, url=None):
    published = published or _dt(9, 20)
    return Article(f"a{i}", source, url or f"http://x/{i}", title, "", published, published, "global", "rss", source_id=source)


# --- window ---------------------------------------------------------------------------------------------------------------------
def test_window_defaults_to_previous_weekday_1600_through_0900():
    s = Settings()
    tue = news_window(date(2026, 11, 10), s)
    mon = news_window(date(2026, 11, 9), s)
    assert (tue[0], tue[1]) == (_dt(9, 15, 30), _dt(10, 8, 30))
    assert (mon[0], mon[1]) == (_dt(6, 15, 30), _dt(9, 8, 30))  # Monday reaches back to Friday 15:30: the weekend is covered


def test_calendar_day_mode_is_literally_yesterday():
    mon = news_window(date(2026, 11, 9), Settings(window_previous_day="calendar_day"))
    assert mon[0] == _dt(8, 15, 30)  # Sunday 15:30


# --- archive window + ingest ----------------------------------------------------------------------------------------------------
def test_in_window_ignores_when_we_ingested_unlike_the_point_in_time_replay(tmp_path):
    archive = ArticleArchive(tmp_path / "a.sqlite3")
    late = Article("l", "Reuters", "http://x/l", "ingested late", "", _dt(9, 20), _dt(10, 14), "global", "rss")
    archive.record([late, _art(2, "on time", published=_dt(9, 21))])
    start, cutoff = _dt(9, 16), _dt(10, 9)
    assert {a.title for a in archive.in_window(start, cutoff)} == {"ingested late", "on time"}
    assert {a.title for a in archive.as_of(start, cutoff, 60)} == {"on time"}  # replay: not knowable at 08:30


def test_ingest_job_adds_only_new_items_and_reports_counts(tmp_path):
    settings = Settings(data_dir=tmp_path)
    batches = [[_art(1, "one"), _art(2, "two")], [_art(2, "two"), _art(3, "three")]]
    seen = []

    def fetch(feeds, since, until, key, timeout):
        seen.append((since, until))
        return batches.pop(0)

    now = _dt(10, 6)
    first = run_ingest(settings, now=now, fetch=fetch)
    second = run_ingest(settings, now=now + timedelta(minutes=30), fetch=fetch)
    assert (first["fetched"], first["new"], first["archive_total"]) == (2, 2, 2)
    assert (second["fetched"], second["new"], second["archive_total"]) == (2, 1, 3)  # one duplicate
    assert seen[0][0] == now - timedelta(hours=settings.ingest_lookback_hours)


def test_a_live_run_uses_everything_the_ingest_job_collected_not_just_the_current_fetch(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, embedder="tfidf", llm_api_key="", gemini_api_key="", feeds_path=tmp_path / "none.yaml")
    ArticleArchive(settings.archive_file()).record([
        _art(1, "Brent crude oil tops $100 as supply fears grow", published=_dt(9, 18)),    # ingested earlier, inside the window
        _art(2, "RBI keeps repo rate unchanged at 5.25%", published=_dt(10, 7)),
        _art(3, "published before the window opened", published=_dt(9, 10)),
        _art(4, "published after the cutoff", published=_dt(10, 10)),
    ])
    monkeypatch.setattr(orchestrator, "fetch_headlines", lambda *a, **k: [])  # the feeds returned nothing right now
    result = orchestrator.run_signal(date(2026, 11, 10), settings)
    assert result.article_count_fetched == 2  # only the two inside 15:30 -> 08:30
    assert "2 in the 09 Nov 15:30-10 Nov 08:30 IST window incl. earlier ingests" in result.stage_status["2a_headlines"]


# --- per-seed report ------------------------------------------------------------------------------------------------------------
def _fixture_report():
    rep = _art(1, "Brent crude oil crosses $115 after Houthi strike on Saudi sites", source="Reuters")
    copy1 = _art(2, "Brent tops $115 on Saudi attack", source="CNBC")
    copy2 = _art(3, "Oil surges past $115", source="Bloomberg")
    other = _art(4, "India October IIP slows to 6.7%", source="Economic Times")
    clusters = {
        "c1": EventCluster("c1", rep, (rep, copy1, copy2)),
        "c2": EventCluster("c2", other, (other,)),
    }
    by_id = {s.seed_id: s for s in SEEDS}
    rel = [
        RelevantArticle(rep, "E05", by_id["E05"].family, "INTL", 1.0, (), match_confidence=0.78, cluster_id="c1", cluster_size=3, cluster_sources=3),
        RelevantArticle(rep, "G02", by_id["G02"].family, "INTL", 1.0, (), match_confidence=0.54, cluster_id="c1", cluster_size=3, cluster_sources=3),
        RelevantArticle(other, "D03", by_id["D03"].family, "Domestic", 1.0, (), match_confidence=0.6, cluster_id="c2"),
    ]

    def j(art, seed, direction, intensity, status="scored", cluster="c1"):
        return ImpactJudgement(art.article_id, seed, "fam", direction, 0.8, intensity, "[summary] llm: " + art.title[:20], "m",
                               shock_group=by_id[seed].shock_group, cluster_id=cluster,
                               details={"status": status, "provenance": "reported", "surprise": "unknown", "source_weight": 1.0})

    judg = [j(rep, "E05", -1, 0.7), j(rep, "G02", -1, 0.5), j(other, "D03", 0, 0.0, status="priced_in", cluster="c2")]
    rows = [
        {"seed_id": "E05", "article_ids": ["a1"], "signal": -0.7, "counted": -0.7, "event_weight": 1.0},
        {"seed_id": "G02", "article_ids": ["a1"], "signal": -0.125, "counted": -0.06, "event_weight": 0.25},
    ]
    return build_seed_report(SEEDS, rel, judg, rows, clusters)


def test_report_lists_every_seed_and_dedupes_each_story_with_its_other_outlets():
    report = _fixture_report()
    assert len(report) == len(SEEDS) == 61
    by_id = {s["seed_id"]: s for s in report}
    e05 = by_id["E05"]
    assert e05["score"] < -0.5 and e05["events_judged"] == 1 and e05["events_scored"] == 1
    art = e05["articles"][0]
    assert art["source"] == "Reuters" and art["event_headlines"] == 3 and art["event_outlets"] == 3
    assert sorted(art["other_outlets"]) == ["Bloomberg", "CNBC"] and len(art["related"]) == 2 and art["contribution"] == -0.7
    assert by_id["R01"]["score"] == 0.0 and by_id["R01"]["articles"] == []  # a seed with no news is still listed
    assert by_id["G02"]["articles"][0]["event_weight"] == 0.25  # the secondary seed of the same event
    assert by_id["D03"]["articles"][0]["status"] == "priced_in" and by_id["D03"]["score"] == 0.0


def test_include_modes_choose_which_judged_items_are_listed():
    report = _fixture_report()
    counts = {mode: sum(len(s["articles"]) for s in filter_report(report, mode)) for mode in ("scored", "relevant", "all")}
    assert counts == {"scored": 2, "relevant": 3, "all": 3}
    assert len(filter_report(report, "scored")) == 61  # seeds are always kept
    with pytest.raises(ValueError):
        filter_report(report, "nope")


# --- CSV -----------------------------------------------------------------------------------------------------------------------
def _payload():
    return {"target_date": "2026-11-10", "window_start": _dt(9, 16).isoformat(), "window_end": _dt(10, 9).isoformat(), "score": -0.42,
            "direction": "negative", "confidence": 0.55, "run_id": "2026-11-10/090500", "seeds": _fixture_report()}


def test_csv_has_a_bom_all_seeds_and_the_final_score_on_every_row():
    raw = signal_to_csv(_payload())
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert all(r["headline"] for r in rows) and "R01" not in {r["seed_id"] for r in rows}  # seeds with no news have no row
    assert {r["final_score"] for r in rows} == {"-0.42"} and {r["run_id"] for r in rows} == {"2026-11-10/090500"}
    e05 = next(r for r in rows if r["seed_id"] == "E05")
    assert e05["headline"].startswith("Brent crude oil crosses") and e05["other_outlets"] == "Bloomberg; CNBC" and e05["event_outlets"] == "3"
    contributions = [abs(float(r["contribution_to_final"])) for r in rows]
    assert contributions == sorted(contributions, reverse=True)  # biggest contributors first
    scored = list(csv.DictReader(io.StringIO(signal_to_csv(_payload(), include="scored").decode("utf-8-sig"))))
    assert sum(1 for r in rows if r["headline"]) == 3 and sum(1 for r in scored if r["headline"]) == 2  # priced-in D03 only under "relevant"


def test_seed_view_is_one_row_per_seed():
    rows = list(csv.DictReader(io.StringIO(signal_to_csv(_payload(), view="seeds").decode("utf-8-sig"))))
    assert len(rows) == 61 and next(r for r in rows if r["seed_id"] == "E05")["top_headline"].startswith("Brent")
    with pytest.raises(ValueError):
        signal_to_csv(_payload(), view="x")


# --- async jobs -----------------------------------------------------------------------------------------------------------------
def _wait_for(manager, job_id, status, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        job = manager.get(job_id)
        if job["status"] == status:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job never reached {status}: {manager.get(job_id)}")


def test_a_job_runs_in_the_background_and_a_second_request_is_refused_until_it_finishes(tmp_path):
    release = threading.Event()
    calls = []

    def runner(settings, target, replay, cutoff_time, persist):
        calls.append((target, replay, cutoff_time, persist))
        release.wait(5)
        return {"run_id": "r", "score": 0.1, "direction": "positive", "confidence": 0.5, "warnings": ["w"]}

    manager = JobManager(tmp_path, runner)
    job = manager.submit(Settings(data_dir=tmp_path), date(2026, 11, 10))
    assert job["status"] in {"queued", "running"}
    _wait_for(manager, job["job_id"], "running")
    with pytest.raises(JobBusy) as busy:
        manager.submit(Settings(data_dir=tmp_path), date(2026, 11, 10))
    assert busy.value.job["job_id"] == job["job_id"]
    release.set()
    done = _wait_for(manager, job["job_id"], "succeeded")
    assert done["score"] == 0.1 and done["warnings"] == 1 and done["csv_url"] == "/v1/signal/2026-11-10/csv"
    manager.submit(Settings(data_dir=tmp_path), date(2026, 11, 11))  # free again


def test_a_failed_run_is_reported_not_raised_and_frees_the_slot(tmp_path):
    def boom(*args):
        raise RuntimeError("LLM exploded")

    manager = JobManager(tmp_path, boom)
    job, result = manager.run_now(Settings(data_dir=tmp_path), date(2026, 11, 10))
    assert job["status"] == "failed" and "LLM exploded" in job["error"] and result is None
    manager2 = JobManager(tmp_path, lambda *a: {"score": 0.0, "direction": "neutral", "confidence": 0.0})
    assert manager2.run_now(Settings(data_dir=tmp_path), date(2026, 11, 10))[0]["status"] == "succeeded"


def test_jobs_left_running_by_a_restart_are_marked_failed(tmp_path):
    stuck = JobManager(tmp_path, lambda *a: {})
    job = stuck._claim(date(2026, 11, 10), False, None)  # simulates a process that died mid-run
    reborn = JobManager(tmp_path, lambda *a: {})
    again = reborn.get(job["job_id"])
    assert again["status"] == "failed" and "restarted" in again["error"]
    assert [j["job_id"] for j in reborn.list()] == [job["job_id"]]


# --- API ------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path):
    release = threading.Event()
    release.set()

    def runner(settings, target, replay, cutoff_time, persist):
        payload = {**_payload(), "target_date": target.isoformat(), "judge_summary": {}, "warnings": []}
        if persist:
            FileSignalStore(settings.data_dir).save(target, payload)
        runner.gate.wait(5)
        return payload

    runner.gate = release
    app = create_app()
    app.state.job_runner = runner
    app.dependency_overrides[get_settings] = lambda: Settings(api_keys="k", allow_unauthenticated=False, data_dir=tmp_path,
                                                              feeds_path=tmp_path / "none.yaml")
    client = TestClient(app)
    client.runner = runner
    return client


H = {"X-API-Key": "k"}


def _poll(client, job_id, status="succeeded"):
    end = time.time() + 5
    while time.time() < end:
        job = client.get(f"/v1/runs/{job_id}", headers=H).json()
        if job["status"] == status:
            return job
        time.sleep(0.02)
    raise AssertionError(job)


def test_post_run_is_asynchronous_then_results_and_csv_are_downloadable(api):
    started = api.post("/v1/signal/run", headers=H, json={"target_date": "2026-11-10"})
    assert started.status_code == 202 and started.json()["status"] in {"queued", "running"}
    job = _poll(api, started.json()["job_id"])
    assert job["result_url"] == "/v1/signal/2026-11-10" and job["score"] == -0.42

    data = api.get("/v1/signal/2026-11-10", headers=H).json()
    assert data["score"] == -0.42 and len(data["seeds"]) == 61
    assert sum(len(s["articles"]) for s in data["seeds"]) == 3  # include=relevant is the default
    assert sum(len(s["articles"]) for s in api.get("/v1/signal/2026-11-10?include=scored", headers=H).json()["seeds"]) == 2

    csv_response = api.get("/v1/signal/2026-11-10/csv", headers=H)
    assert csv_response.status_code == 200 and csv_response.headers["content-type"].startswith("text/csv")
    assert 'filename="news_signal_2026-11-10.csv"' in csv_response.headers["content-disposition"]
    assert csv_response.content.startswith(b"\xef\xbb\xbf") and b"final_score" in csv_response.content
    seeds_csv = api.get("/v1/signal/2026-11-10/csv?view=seeds", headers=H)
    assert 'news_signal_2026-11-10_seeds.csv' in seeds_csv.headers["content-disposition"]
    assert api.get("/v1/runs", headers=H).json()[0]["job_id"] == job["job_id"]


def test_a_second_run_while_one_is_in_progress_gets_409_with_the_running_job(api):
    api.runner.gate.clear()
    first = api.post("/v1/signal/run", headers=H, json={"target_date": "2026-11-10"}).json()
    second = api.post("/v1/signal/run", headers=H, json={"target_date": "2026-11-11"})
    assert second.status_code == 409 and second.json()["detail"]["job"]["job_id"] == first["job_id"]
    api.runner.gate.set()
    _poll(api, first["job_id"])


def test_wait_true_returns_the_signal_directly_and_bad_input_is_rejected(api):
    done = api.post("/v1/signal/run", headers=H, json={"target_date": "2026-11-10", "wait": True})
    assert done.status_code == 200 and done.json()["score"] == -0.42
    assert api.post("/v1/signal/run", headers=H, json={"target_date": "2026-11-10", "cutoff_time": "nine"}).status_code == 422
    assert api.get("/v1/signal/2026-11-10?include=bogus", headers=H).status_code == 422
    assert api.get("/v1/signal/2026-11-10/csv?view=bogus", headers=H).status_code == 422
    assert api.get("/v1/signal/2025-01-01", headers=H).status_code == 404
    assert api.get("/v1/signal/2025-01-01/csv", headers=H).status_code == 404
    assert api.get("/v1/runs/nope", headers=H).status_code == 404
    assert api.get("/v1/signal/2026-11-10/csv").status_code == 401  # the download needs the key too


def test_ingest_endpoint(api, monkeypatch):
    monkeypatch.setattr("app.api.routes.run_ingest", lambda settings: {"fetched": 5, "new": 3, "archive_total": 9})
    assert api.post("/v1/ingest", headers=H).json() == {"fetched": 5, "new": 3, "archive_total": 9}


# --- cron trigger ---------------------------------------------------------------------------------------------------------------
class FakeHttp:
    def __init__(self, script):
        self.script, self.requests = list(script), []

    def __call__(self, request, timeout=None):
        self.requests.append((request.method, request.full_url, request.get_header("X-api-key")))
        status, body = self.script.pop(0)
        if status >= 400:
            import urllib.error
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(body))

        class R:
            def __init__(self):
                self.status = status

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


def test_trigger_run_starts_polls_and_saves_the_csv(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_URL", "https://svc.example")
    monkeypatch.setenv("TRIGGER_API_KEY", "secret")
    http = FakeHttp([
        (202, json.dumps({"job_id": "j1", "status": "queued"}).encode()),
        (200, json.dumps({"job_id": "j1", "status": "running"}).encode()),
        (200, json.dumps({"job_id": "j1", "status": "succeeded", "score": -0.4, "direction": "negative", "confidence": 0.5, "warnings": 1}).encode()),
        (200, b"csv-bytes"),
    ])
    lines = []
    target = tmp_path / "out.csv"
    code = trigger.run_trigger(["run", "--date", "2026-11-10", "--csv", str(target), "--poll", "0"], opener=http, sleep=lambda s: None, out=lines.append)
    assert code == 0 and target.read_bytes() == b"csv-bytes"
    assert [m for m, _, _ in http.requests] == ["POST", "GET", "GET", "GET"] and all(k == "secret" for _, _, k in http.requests)
    assert http.requests[0][1] == "https://svc.example/v1/signal/run" and any("score -0.4000" in l for l in lines)


def test_trigger_reports_busy_failed_and_missing_configuration(monkeypatch):
    monkeypatch.setenv("SERVICE_URL", "https://svc.example")
    monkeypatch.setenv("TRIGGER_API_KEY", "secret")
    lines = []
    assert trigger.run_trigger(["run"], opener=FakeHttp([(409, b'{"detail":"busy"}')]), sleep=lambda s: None, out=lines.append) == 1
    failed = FakeHttp([(202, b'{"job_id":"j","status":"queued"}'), (200, b'{"job_id":"j","status":"failed","error":"quota"}')])
    assert trigger.run_trigger(["run", "--poll", "0"], opener=failed, sleep=lambda s: None, out=lines.append) == 1 and any("quota" in l for l in lines)
    assert trigger.run_trigger(["ingest"], opener=FakeHttp([(200, b'{"new": 4}')]), out=lines.append) == 0
    monkeypatch.delenv("SERVICE_URL")
    assert trigger.run_trigger(["ingest"], out=lines.append) == 2


# --- scheduler + publish ----------------------------------------------------------------------------------------------------------
def test_scheduler_decides_what_is_due_each_minute():
    from app.scheduler import due_actions

    s = Settings()
    assert due_actions(_dt(10, 8, 30), s) == ["ingest", "run"]        # Tuesday 08:30 IST: the half-hour ingest and the daily run
    assert due_actions(_dt(10, 8, 0), s) == ["ingest"]
    assert due_actions(_dt(10, 8, 15), s) == []
    assert due_actions(_dt(14, 8, 30), s) == ["ingest"]               # Saturday: still ingests (Monday's window needs the weekend), no run
    assert due_actions(_dt(10, 3, 0).astimezone(ZoneInfo("UTC")), s) == ["ingest"]  # tz-independent: 03:00 IST, not 08:30


def test_scheduler_ticks_once_per_minute_and_survives_failures(tmp_path):
    from app.scheduler import Scheduler

    started, clock = [], [_dt(10, 8, 30)]
    manager = JobManager(tmp_path, lambda *a: started.append(a) or {"score": 0.0, "direction": "neutral", "confidence": 0.0})
    boom = {"n": 0}

    def ingest(settings):
        boom["n"] += 1
        raise RuntimeError("feed down")

    sched = Scheduler(Settings(data_dir=tmp_path), manager, ingest=ingest, clock=lambda: clock[0])
    assert sched.tick() == ["run"]            # ingest failed (logged, not raised), run started
    assert sched.tick() == []                 # same minute again: nothing
    _wait_for(manager, manager.list()[0]["job_id"], "succeeded")
    clock[0] = _dt(10, 9, 0)
    assert sched.tick() == []                 # 09:00: ingest raised again, no run
    assert boom["n"] == 2 and len(started) == 1


def test_publish_writes_the_score_on_the_row_whose_next_trade_date_is_the_trade_date():
    from app.publish.prediction_table import publish_scores

    executed = []

    class Cur:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))
            self.rowcount = 0 if params and params[-1] == date(2026, 11, 10) else 1

    class Conn:
        committed = False

        def cursor(self):
            return Cur()

        def commit(self):
            self.committed = True

        def close(self):
            pass

    conn = Conn()
    settings = Settings(news_score_db_url="postgres://x")
    out = publish_scores(settings, [{"trade_date": date(2026, 11, 12), "score": 0.16, "direction": "positive", "confidence": 0.5, "run_id": "r"},
                                    {"trade_date": date(2026, 11, 10), "score": 0.37, "direction": "positive", "confidence": 0.5, "run_id": "r"}],
                         connect=lambda url: conn)
    assert [(o["trade_date"], o["written"]) for o in out] == [("2026-11-12", True), ("2026-11-10", False)]  # holiday: no row, reported
    updates = [e for e in executed if e[0].startswith("UPDATE")]
    assert "next_trade_date = %s" in updates[0][0] and updates[0][1][0] == 0.16 and conn.committed
    assert sum(1 for e in executed if "ADD COLUMN IF NOT EXISTS news_score double" in e[0]) == 1
    def never_connect(url):  # a bare Settings() would read the developer's real .env and write to the live database
        raise AssertionError("publish_scores connected although no database is configured")

    unconfigured = Settings(_env_file=None, news_score_db_url="", supabase_conn_str="")
    assert publish_scores(unconfigured, [{"trade_date": date(2026, 11, 12), "score": 0.1}], connect=never_connect)[0]["written"] is False


def test_the_backfill_csv_is_published_by_trade_date(tmp_path):
    from app.publish.prediction_table import scores_from_backfill_csv

    path = tmp_path / "s.csv"
    path.write_text("date,score,direction,confidence,run_id\n2026-11-11,-0.14,negative,0.6,a\n2026-11-12,0.16,positive,0.5,b\n2026-11-13,,x,,c\n", encoding="utf-8")
    rows = scores_from_backfill_csv(path, start=date(2026, 11, 12))
    assert [(r["trade_date"], r["score"]) for r in rows] == [(date(2026, 11, 12), 0.16)]
