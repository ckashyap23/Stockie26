"""Company-level seeds count only when they name a NIFTY heavyweight, scaled by index weight.

Rules live in data/nifty_heavyweights.yaml (editable). See that file for the scaling formula.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Heavyweight:
    name: str
    weight_pct: float
    aliases: tuple[str, ...]


class CompanyRules:
    def __init__(self, heavyweights: list[Heavyweight], required_seeds: frozenset[str], full_weight_pct: float) -> None:
        self.required_seeds = required_seeds
        self.full_weight_pct = full_weight_pct
        self._patterns = [
            (hw, re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(a.lower()) for a in hw.aliases) + r")(?![a-z0-9])"))
            for hw in heavyweights
            if hw.aliases
        ]

    @classmethod
    def load(cls, path: Path) -> "CompanyRules":
        if not path.exists():
            return cls([], frozenset(), 5.0)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        heavyweights = [
            Heavyweight(str(h["name"]), float(h.get("weight_pct", 0.0)), tuple(str(a) for a in h.get("aliases", [])))
            for h in raw.get("heavyweights", [])
        ]
        return cls(heavyweights, frozenset(raw.get("required_seeds") or ()), float(raw.get("full_weight_pct", 5.0)))

    def applies_to(self, seed_id: str) -> bool:
        return seed_id in self.required_seeds

    def named(self, text: str) -> list[Heavyweight]:
        text = text.lower()
        return [hw for hw, pattern in self._patterns if pattern.search(text)]

    def weight_scale(self, named: list[Heavyweight]) -> float:
        """Largest named weight, relative to `full_weight_pct`, capped at 1."""
        if not named or self.full_weight_pct <= 0:
            return 0.0
        return min(1.0, max(hw.weight_pct for hw in named) / self.full_weight_pct)
