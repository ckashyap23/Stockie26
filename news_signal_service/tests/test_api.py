import json

import pytest
from fastapi.testclient import TestClient

from app.config import SERVICE_ROOT, Settings, get_settings
from app.main import create_app

SERVICE_FEEDS = SERVICE_ROOT / "data" / "feeds.yaml"


@pytest.fixture
def client(tmp_path):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        api_keys="secret", allow_unauthenticated=False, data_dir=tmp_path, feeds_path=tmp_path / "none.yaml"
    )
    return TestClient(app)


def test_health_is_open(client):
    assert client.get("/health").json()["status"] == "ok"


def test_v1_requires_api_key(client):
    assert client.get("/v1/events").status_code == 401
    assert client.get("/v1/events", headers={"X-API-Key": "wrong"}).status_code == 401


def test_events_and_sources_listing(client):
    client.app.dependency_overrides[get_settings] = lambda: Settings(
        api_keys="secret", allow_unauthenticated=False, feeds_path=SERVICE_FEEDS
    )
    headers = {"X-API-Key": "secret"}
    assert len(client.get("/v1/events", headers=headers).json()) == 61
    assert len(client.get("/v1/sources", headers=headers).json()) == 18


def test_run_then_read_roundtrip_with_no_feeds(client):
    headers = {"X-API-Key": "secret"}
    assert client.get("/v1/signal", headers=headers).status_code == 404
    run = client.post("/v1/signal/run", headers=headers, json={"target_date": "2026-01-05", "wait": True})
    body = run.json()
    assert run.status_code == 200
    assert body["direction"] == "neutral" and body["score"] == 0.0
    assert body["calibrated"] is False and body["magnitude_basis"] == "uncalibrated_score"
    assert client.get("/v1/signal?target_date=2026-01-05", headers=headers).json()["target_date"] == "2026-01-05"


def test_run_writes_one_dated_folder_per_run_with_every_stage(client, tmp_path):
    headers = {"X-API-Key": "secret"}
    first = client.post("/v1/signal/run", headers=headers, json={"target_date": "2026-01-05", "wait": True}).json()
    second = client.post("/v1/signal/run", headers=headers, json={"target_date": "2026-01-05", "wait": True}).json()
    assert first["run_id"] != second["run_id"] and first["run_id"].startswith("2026-01-05/")
    folder = tmp_path / "runs" / first["run_id"]
    names = {p.name for p in folder.iterdir()}
    assert {"meta.json", "headlines.jsonl", "clusters.jsonl", "matches.jsonl", "fulltext.jsonl",
            "judgements.jsonl", "aggregation.json", "signal.json"} <= names
    import json
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert "hf_token" not in meta["settings"] and "api_keys" not in meta["settings"]
    assert meta["file_hashes"]["event_seeds"] and "5_aggregate" in meta["stage_seconds"]
    # persist=false leaves no folder
    client.post("/v1/signal/run", headers=headers, json={"target_date": "2026-01-06", "persist": False, "wait": True})
    assert not (tmp_path / "runs" / "2026-01-06").exists()


def test_dropped_broker_calls_are_logged_in_the_run_folder(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.config import Settings
    from app.pipeline import orchestrator
    from app.pipeline.ingest import RssFeed  # noqa: F401
    from app.domain import Article

    tz = ZoneInfo("Asia/Kolkata")
    pub = datetime(2026, 1, 5, 7, 0, tzinfo=tz)
    arts = [
        Article("a", "ET", "http://x/a", "Jefferies upgrades Infosys to buy, target price Rs 2,000 after Q2 results", "", pub, pub, "india", "rss"),
        Article("b", "ET", "http://x/b", "HDFC Bank Q2 profit jumps, beats estimates", "", pub, pub, "india", "rss"),
    ]
    orig = orchestrator.fetch_headlines
    orchestrator.fetch_headlines = lambda *a, **k: arts
    try:
        settings = Settings(data_dir=tmp_path, feeds_path=SERVICE_FEEDS)
        result = orchestrator.run_signal(datetime(2026, 1, 5).date(), settings, record=True)
    finally:
        orchestrator.fetch_headlines = orig
    rows = [json.loads(line) for line in (tmp_path / "runs" / result.run_id / "commentary_dropped.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["article_id"] for r in rows] == ["a"]
    assert rows[0]["category"] == "broker_call" and isinstance(rows[0]["would_have_matched"], list)  # which seeds it WOULD have hit depends on the gate
    assert "1 non-event/broker headlines filtered out" in result.stage_status["2c_relevance"]
