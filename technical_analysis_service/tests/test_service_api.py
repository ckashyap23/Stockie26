from types import SimpleNamespace

from technical_analysis_service.app import create_app
from technical_analysis_service.jobs import JobStore
from technical_analysis_service import jobs as jobs_module
from technical_analysis_service import dashboard


def test_api_protects_dashboard_and_jobs_but_keeps_health_public(monkeypatch, tmp_path):
    monkeypatch.setenv("TECHNICAL_ANALYSIS_API_KEY", "test-secret")
    app = create_app(job_store=JobStore(tmp_path))
    client = app.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/research").status_code == 401
    assert client.post("/api/v1/jobs", json={"task": "paper.entry"}).status_code == 401
    response = client.get("/api/v1/tasks", headers={"Authorization": "Bearer test-secret"})
    assert response.status_code == 200
    assert {"research", "production.predict", "production.signal", "paper.entry", "paper.monitor"} <= set(response.json["tasks"])


def test_without_a_key_everything_but_health_is_refused_unless_explicitly_opened(monkeypatch, tmp_path):
    monkeypatch.delenv("TECHNICAL_ANALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED", raising=False)
    client = create_app(job_store=JobStore(tmp_path)).test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/research").status_code == 503
    assert client.post("/api/v1/jobs", json={"task": "paper.entry"}).status_code == 503
    monkeypatch.setenv("TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED", "1")
    assert client.get("/api/v1/tasks").status_code == 200


def test_jobs_validate_request_serialize_and_persist(monkeypatch, tmp_path):
    monkeypatch.delenv("TECHNICAL_ANALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    store = JobStore(tmp_path)
    client = create_app(job_store=store).test_client()
    assert client.post("/api/v1/jobs", json=[]).status_code == 400
    assert client.post("/api/v1/jobs", json={"task": "arbitrary.module"}).status_code == 400
    assert client.post("/api/v1/jobs", json={"task": "research", "args": "--help"}).status_code == 400
    response = client.post("/api/v1/jobs", json={"task": "research", "args": ["--help"]})
    assert response.status_code == 202
    job_id = response.json["id"]
    assert client.get(response.headers["Location"]).json["state"] == "running"
    assert client.post("/api/v1/jobs", json={"task": "paper.entry"}).status_code == 409
    store._finish(job_id, 0)
    assert JobStore(tmp_path).get(job_id)["state"] == "done"
    assert client.get("/api/v1/jobs/unknown").status_code == 404


def test_job_failure_is_recorded_and_releases_writer(monkeypatch, tmp_path):
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    store = JobStore(tmp_path / "jobs")
    job = store.submit("production.predict", ["--start", "2026-01-01"])
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write("controlled failure")
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(jobs_module.subprocess, "run", run)
    store._run(job["id"], "technical_analysis_service.scripts.daily_NIFTY.daily_nifty_prediction", job["args"])
    result = store.get(job["id"])
    assert result["state"] == "failed" and result["exit_code"] == 2
    assert "controlled failure" in result["log_tail"]
    assert calls[0][0][1:3] == ["-m", "technical_analysis_service.scripts.daily_NIFTY.daily_nifty_prediction"]
    assert "shell" not in calls[0][1]
    assert store.submit("paper.report", ["--help"])["state"] == "running"


def test_production_and_paper_read_api_validate_dates(monkeypatch, tmp_path):
    monkeypatch.delenv("TECHNICAL_ANALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setattr(dashboard, "load_production_signal_rows", lambda start, end: ([{"effective_prediction": "PUT"}], ""))
    monkeypatch.setattr(dashboard, "load_paper_trade_rows", lambda date: ([{"status": "OPEN"}], ""))
    client = create_app(job_store=JobStore(tmp_path)).test_client()
    assert client.get("/api/v1/production?start=bad").status_code == 400
    assert client.get("/api/v1/production?start=2026-02-01&end=2026-01-01").status_code == 400
    assert client.get("/api/v1/production").json["rows"][0]["effective_prediction"] == "PUT"
    assert client.get("/api/v1/paper/trades?trade_date=bad").status_code == 400
    assert client.get("/api/v1/paper/trades").json["rows"][0]["status"] == "OPEN"
    strategies = client.get("/api/v1/strategies").json["strategies"]
    assert {"SIGNAL", "RESEARCH"} <= {item["strategy_type"] for item in strategies}
