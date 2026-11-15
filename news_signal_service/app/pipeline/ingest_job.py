"""Ingest job: fetch the feeds into the timestamped article archive. No LLM, no scoring: a few seconds.

Run it every ~30 minutes (Railway cron -> `python -m app.trigger ingest`, or POST /v1/ingest). Feeds only keep their latest 30-60 items,
so without this a run at 08:30 could not see the evening's news; with it the 15:30-08:30 window is complete and reproducible.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.pipeline.ingest import fetch_headlines, load_feeds
from app.storage.archive import ArticleArchive

IST = ZoneInfo("Asia/Kolkata")


def run_ingest(settings: Settings, now: datetime | None = None, fetch=fetch_headlines) -> dict:
    now = now or datetime.now(IST)
    archive = ArticleArchive(settings.archive_file())
    before = archive.stats()["articles"]
    feeds = load_feeds(settings.feeds_path)
    fetched = fetch(feeds, now - timedelta(hours=settings.ingest_lookback_hours), now, settings.newsapi_key, settings.http_timeout_seconds)
    archive.record(fetched)
    after = archive.stats()
    return {
        "ingested_at": now.isoformat(),
        "feeds": len(feeds),
        "fetched": len(fetched),
        "new": after["articles"] - before,
        "archive_total": after["articles"],
        "archive_earliest": after["earliest"],
        "archive_latest": after["latest"],
    }
