"""Timestamped article archive (SQLite).

Why: a pre-open forecast may only use what was public *and known to us* before the cutoff, and a
backtest must be able to replay "as of 08:30 on date D". RSS has no history, so every run records
what it saw. For each article we keep:

  first_published  earliest publication time ever seen (never moves later)
  first_ingested   when we first saw it
  last_published   latest feed timestamp (a later value means the publisher revised it)
  updated_at       when we first noticed a revision
  versions         every (seen_at, published_at, title) observation

Eligibility for a cutoff C: first_published <= C and first_ingested <= C. A revision after C can
never leak into a forecast for C because only the first-seen values are used.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain import Article

IST = ZoneInfo("Asia/Kolkata")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  article_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, source_priority TEXT, body_mode TEXT,
  url TEXT, title TEXT, summary TEXT, region TEXT, provider TEXT,
  first_published TEXT, first_ingested TEXT, last_published TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS article_versions (
  article_id TEXT, seen_at TEXT, published_at TEXT, title TEXT
);
CREATE INDEX IF NOT EXISTS ix_articles_first_published ON articles(first_published);
"""


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(IST).isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class ArticleArchive:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self):
        """Committed AND closed on exit (sqlite3's own context manager never closes the connection)."""
        return _Conn(self._path)

    def record(self, articles: list[Article]) -> list[Article]:
        """Upsert what this run saw; return each article with its canonical first-seen timestamps."""
        out: list[Article] = []
        with self._connect() as db:
            for article in articles:
                row = db.execute("SELECT * FROM articles WHERE article_id = ?", (article.article_id,)).fetchone()
                seen_at = _iso(article.fetched_at)
                published = _iso(article.published_at)
                if row is None:
                    db.execute(
                        "INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (article.article_id, article.source, article.source_id, article.source_priority,
                         article.body_mode, article.url, article.title, article.summary, article.region,
                         article.provider, published, seen_at, published, None),
                    )
                    db.execute("INSERT INTO article_versions VALUES (?,?,?,?)",
                               (article.article_id, seen_at, published, article.title))
                    out.append(article)
                    continue
                first_pub = min(row["first_published"], published)
                updated = row["updated_at"]
                if published != row["last_published"] or article.title != row["title"]:
                    # Publisher revised the item: remember it, but never move first_published later.
                    updated = updated or seen_at
                    db.execute("INSERT INTO article_versions VALUES (?,?,?,?)",
                               (article.article_id, seen_at, published, article.title))
                    db.execute("UPDATE articles SET last_published=?, updated_at=? WHERE article_id=?",
                               (max(row["last_published"], published), updated, article.article_id))
                if first_pub != row["first_published"]:
                    db.execute("UPDATE articles SET first_published=? WHERE article_id=?", (first_pub, article.article_id))
                out.append(replace(
                    article,
                    published_at=_parse(first_pub),
                    fetched_at=_parse(row["first_ingested"]),
                    updated_at=_parse(updated),
                ))
        return out

    @staticmethod
    def _to_article(row) -> Article:
        return Article(
            row["article_id"], row["source"], row["url"], row["title"], row["summary"],
            _parse(row["first_published"]), _parse(row["first_ingested"]), row["region"], row["provider"],
            updated_at=_parse(row["updated_at"]), source_id=row["source_id"] or "",
            body_mode=row["body_mode"] or "full", source_priority=row["source_priority"] or "P2",
        )

    def in_window(self, window_start: datetime, cutoff: datetime) -> list[Article]:
        """Everything first published in [window_start, cutoff], whenever we ingested it (used by live runs and ad-hoc triggers;
        point-in-time replays use `as_of`, which also requires the item to have been ingested by then)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM articles WHERE first_published >= ? AND first_published <= ? ORDER BY first_published",
                (_iso(window_start), _iso(cutoff)),
            ).fetchall()
        return [self._to_article(row) for row in rows]

    def as_of(self, window_start: datetime, cutoff: datetime, ingest_grace_minutes: int = 60) -> list[Article]:
        """Articles knowable for `cutoff`: first published in [window_start, cutoff] and first ingested by
        cutoff + grace (the daily run itself starts after the cutoff, so a zero grace would drop everything)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM articles WHERE first_published >= ? AND first_published <= ? AND first_ingested <= ? "
                "ORDER BY first_published",
                (_iso(window_start), _iso(cutoff), _iso(cutoff + timedelta(minutes=ingest_grace_minutes))),
            ).fetchall()
        return [
            Article(
                row["article_id"], row["source"], row["url"], row["title"], row["summary"],
                _parse(row["first_published"]), _parse(row["first_ingested"]), row["region"], row["provider"],
                updated_at=_parse(row["updated_at"]), source_id=row["source_id"] or "",
                body_mode=row["body_mode"] or "full", source_priority=row["source_priority"] or "P2",
            )
            for row in rows
        ]

    def stats(self) -> dict:
        with self._connect() as db:
            n, lo, hi = db.execute("SELECT COUNT(*), MIN(first_published), MAX(first_published) FROM articles").fetchone()
            revised = db.execute("SELECT COUNT(*) FROM articles WHERE updated_at IS NOT NULL").fetchone()[0]
        return {"articles": n, "revised": revised, "earliest": lo, "latest": hi}


class _Conn:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row

    def __enter__(self) -> sqlite3.Connection:
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._db.commit() if exc_type is None else self._db.rollback()
        finally:
            self._db.close()
