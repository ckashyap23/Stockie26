"""Step 1 - load the pre-seeded macro event list (CSV) and the publisher list (feeds.yaml)."""
from __future__ import annotations

import csv
from pathlib import Path

import yaml

from app.domain import NewsSource, SeedEvent

_CACHE: dict[tuple[str, int], tuple[SeedEvent, ...]] = {}


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(";") if part.strip())


def load_seeds(path: Path) -> tuple[SeedEvent, ...]:
    """Seeds from the CSV. Cached per (path, mtime), so an edited file is picked up without a restart."""
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _CACHE:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        _CACHE[key] = tuple(_seed(row) for row in rows)
    return _CACHE[key]


def _seed(row: dict[str, str]) -> SeedEvent:
    seed_id = row["Seed ID"].strip()
    return SeedEvent(
        seed_id=seed_id,
        family=row["Event family"].strip(),
        geography=row["Geography"].strip(),
        event_type=row["Event type"].strip(),
        priority=row["Implementation priority"].strip(),
        timing=row["Timing"].strip(),
        aliases=_split(row["Seed aliases (expand semantically)"]),
        source_ids=_split(row["Source IDs"]),
        hypothesis=row["NIFTY transmission hypothesis"].strip(),
        guardrail=row["Required guardrail"].strip(),
        llm_guidance=(row.get("LLM guidance") or "").strip(),
        shock_group=(row.get("Shock group") or seed_id).strip(),
        exclude_cues=_split(row.get("Exclude cues", "")),
        context_terms=_split(row.get("Context terms", "")),
    )


def load_sources(path: Path) -> tuple[NewsSource, ...]:
    """Publishers, generated from feeds.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(
        NewsSource(
            source_id=str(p["id"]),
            publisher=str(p["name"]),
            geography=str(p.get("geography", "")),
            priority=str(p.get("priority", "P2")),
            access=str(p.get("access", "")),
            body=str(p.get("body", "full")),
            enabled=bool(p.get("enabled", True)),
            rights=str(p.get("rights", "")),
            notes=str(p.get("notes", "")),
            feeds=tuple(str(f["url"]) for f in p.get("feeds", [])),
        )
        for p in raw.get("publishers", [])
    )
