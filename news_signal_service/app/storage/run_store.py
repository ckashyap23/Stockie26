"""Per-run audit folder: every stage's artifacts, one folder per run.

    DATA_DIR/runs/<target_date>/<HHMMSS>/
        meta.json          settings snapshot (no secrets), file hashes, scorer, window, timings, warnings
        headlines.jsonl    every item inside the window, with first_published / first_ingested / updated_at
        clusters.jsonl     dedupe groups: representative, members, distinct sources
        matches.jsonl      every candidate seed per event: keyword score, embedding, confidence, selected / why not
        fulltext.jsonl     per matched event: content_source and characters read (bodies live in DATA_DIR/cache)
        judgements.jsonl   FinBERT label/confidence, cues hit, direction, confidence, intensity
        aggregation.json   per-cluster gate / rank / decay, per-group and per-family scores
        signal.json        the final output

A run never overwrites another: same-day reruns get their own HHMMSS folder. `signals/<date>.json`
(see file_store) remains the stable "latest" the API reads.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_SECRET_FIELDS = {"api_keys", "newsapi_key"}


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


class RunRecorder:
    def __init__(self, data_dir: Path, target: date, now: datetime | None = None) -> None:
        now = now or datetime.now(IST)
        base = data_dir / "runs" / target.isoformat()
        stamp = now.strftime("%H%M%S")
        folder, n = base / stamp, 1
        while folder.exists():  # two runs within one second
            n += 1
            folder = base / f"{stamp}-{n}"
        folder.mkdir(parents=True)
        self.dir = folder
        self.run_id = f"{target.isoformat()}/{folder.name}"
        self.timings: dict[str, float] = {}

    def write_json(self, name: str, payload) -> None:
        (self.dir / name).write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")

    def write_jsonl(self, name: str, rows) -> None:
        with (self.dir / name).open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")

    def write_meta(self, settings, *, replay: bool, window: tuple[str, str], warnings: list[str], status: dict) -> None:
        snapshot = {k: v for k, v in settings.model_dump(mode="json").items() if k not in _SECRET_FIELDS}
        self.write_json("meta.json", {
            "run_id": self.run_id,
            "recorded_at": datetime.now(IST).isoformat(),
            "replay": replay,
            "window": {"start": window[0], "cutoff": window[1]},
            "python": sys.version.split()[0],
            "settings": snapshot,
            "file_hashes": {
                "event_seeds": file_sha256(settings.seeds_path),
                "feeds": file_sha256(settings.feeds_path),
                "heavyweights": file_sha256(settings.heavyweights_path),
                "commentary_filters": file_sha256(settings.commentary_path),
                "tuning": file_sha256(settings.tuning_path),
            },
            "stage_status": status,
            "stage_seconds": self.timings,
            "warnings": warnings,
        })
