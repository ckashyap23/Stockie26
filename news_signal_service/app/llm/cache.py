"""Cache of parsed LLM judgements (SQLite).

Key = sha256(prompt version | model | seed | headline | text sent | guidance). The same item judged again (a rerun, a replay,
a second run that same morning) costs no call and returns the identical answer, which also keeps free-tier quota for new items.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.llm.prompt import PROMPT_VERSION, ParsedResult


def cache_key(model: str, seed_id: str, headline: str, text: str, guidance: str, window: str = "") -> str:
    """The window is part of the key: whether an item is fresh ("background" or not) depends on where the window starts."""
    raw = "\x1f".join([PROMPT_VERSION, model, seed_id, headline, text, guidance, window])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LlmCache:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, prompt_version TEXT, "
                       "result TEXT, created_at TEXT)")

    def _connect(self):
        """A connection that is committed AND closed on exit (sqlite3's own context manager never closes: locks the file on Windows)."""
        return _Conn(self._path)

    def get(self, key: str) -> ParsedResult | None:
        with self._connect() as db:
            row = db.execute("SELECT result FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return ParsedResult(**json.loads(row[0])) if row else None

    def put(self, key: str, model: str, result: ParsedResult) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?)",
                       (key, model, PROMPT_VERSION, json.dumps(result.to_dict()), datetime.now().isoformat()))


class _Conn:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)

    def __enter__(self) -> sqlite3.Connection:
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._db.commit() if exc_type is None else self._db.rollback()
        finally:
            self._db.close()
