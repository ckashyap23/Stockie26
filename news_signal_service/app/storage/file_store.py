"""Minimal persistence: one JSON file per target date under DATA_DIR/signals/.

On Railway the container filesystem is ephemeral - mount a Volume and point DATA_DIR at it,
or swap this class for a Postgres-backed store (same two methods).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


class FileSignalStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir / "signals"

    def save(self, target: date, payload: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{target.isoformat()}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self, target: date) -> dict | None:
        path = self._dir / f"{target.isoformat()}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def latest(self) -> dict | None:
        if not self._dir.exists():
            return None
        files = sorted(self._dir.glob("*.json"))
        return json.loads(files[-1].read_text(encoding="utf-8")) if files else None
