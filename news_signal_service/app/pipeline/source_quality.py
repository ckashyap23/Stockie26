"""How much to trust a publisher: a weight 0..1 per source, from data/source_quality.yaml (first matching rule wins).

Social-media posts, scraper/aggregator spam and similar sources can carry clickbait or stale claims (a Facebook post saying "RBI raised
the repo rate" while the official outcome was a hold). Their weight multiplies the LLM's confidence, so a low-weight source falls under
`MIN_CONFIDENCE` and scores zero, and when several headlines of one event compete, the more trustworthy source is preferred.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern
    weight: float
    note: str


class SourceQuality:
    def __init__(self, rules: list[_Rule] | None = None, default: float = 1.0) -> None:
        self._rules, self._default = rules or [], default

    @classmethod
    def load(cls, path: Path | None) -> "SourceQuality":
        if path is None or not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rules = [
            _Rule(re.compile(str(r["pattern"]), re.IGNORECASE), float(r["weight"]), str(r.get("note", "")))
            for r in raw.get("rules", [])
        ]
        return cls(rules, float(raw.get("default", 1.0)))

    def weight(self, source: str) -> float:
        for rule in self._rules:
            if rule.pattern.search(source or ""):
                return rule.weight
        return self._default
