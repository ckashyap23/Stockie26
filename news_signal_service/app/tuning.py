"""Judgement tuning knobs loaded from data/tuning.yaml (defaults below apply to any missing key)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "tuning.yaml"

DEFAULTS: dict[str, Any] = {
    "llm": {"batch_size": 8, "content_chars": 1200, "min_relation_confidence": 0.5, "intensity_cap": {"P1": 1.0, "P2": 0.7},
            "provenance_factors": {"official": 1.0, "reported": 1.0, "expectation": 0.3, "unverified": 0.0}},
    "unseeded": {
        "min_hits": 2,
        "match_confidence": 0.4,
        "high_impact_words": ["crash", "plunge", "collapse", "panic", "emergency", "default", "war", "attack", "ban",
                              "shutdown", "outbreak", "sanction", "invasion", "meltdown", "bankruptcy", "halt",
                              "crisis", "tariff", "ceasefire"],
    },
    "relevance": {"alias_in_title": 1.0, "alias_in_summary_only": 0.5, "event_type_word": 0.25, "max_seeds_per_article": 2},
    "similarity": {
        "dense": {"dedupe_threshold": 0.74, "relevance_gate": 0.35},
        "sparse": {"dedupe_threshold": 0.55, "relevance_gate": 0.08},
        "keyword_boost": 0.2,
        "max_events_to_judge": 120,
        "ranking": {"coverage_weight": 0.25, "p2_weight": 0.8},
        "strip_words": ["NIFTY"],
    },
}


@dataclass(frozen=True)
class Tuning:
    llm_batch_size: int
    llm_content_chars: int
    llm_min_relation: float
    llm_intensity_cap: dict[str, float]
    llm_provenance: dict[str, float]
    unseeded_min_hits: int
    unseeded_confidence: float
    unseeded_words: tuple[str, ...]
    alias_in_title: float
    alias_in_summary_only: float
    event_type_word: float
    max_seeds_per_article: int
    dense_dedupe: float
    dense_gate: float
    sparse_dedupe: float
    sparse_gate: float
    keyword_boost: float
    max_events_to_judge: int
    rank_coverage_weight: float
    rank_p2_weight: float
    strip_words: tuple[str, ...]

    def dedupe_threshold(self, kind: str) -> float:
        return self.dense_dedupe if kind == "dense" else self.sparse_dedupe

    def relevance_gate(self, kind: str) -> float:
        return self.dense_gate if kind == "dense" else self.sparse_gate


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        out[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def build_tuning(raw: dict[str, Any] | None) -> Tuning:
    cfg = _merge(DEFAULTS, raw or {})
    llm, un, rel, sim = cfg["llm"], cfg["unseeded"], cfg["relevance"], cfg["similarity"]
    return Tuning(
        llm_batch_size=int(llm["batch_size"]),
        llm_content_chars=int(llm["content_chars"]),
        llm_min_relation=float(llm["min_relation_confidence"]),
        llm_intensity_cap={str(k): float(v) for k, v in llm["intensity_cap"].items()},
        llm_provenance={str(k): float(v) for k, v in llm["provenance_factors"].items()},
        unseeded_min_hits=int(un["min_hits"]),
        unseeded_confidence=float(un["match_confidence"]),
        unseeded_words=tuple(str(w) for w in un["high_impact_words"]),
        alias_in_title=float(rel["alias_in_title"]),
        alias_in_summary_only=float(rel["alias_in_summary_only"]),
        event_type_word=float(rel["event_type_word"]),
        max_seeds_per_article=int(rel["max_seeds_per_article"]),
        dense_dedupe=float(sim["dense"]["dedupe_threshold"]),
        dense_gate=float(sim["dense"]["relevance_gate"]),
        sparse_dedupe=float(sim["sparse"]["dedupe_threshold"]),
        sparse_gate=float(sim["sparse"]["relevance_gate"]),
        keyword_boost=float(sim["keyword_boost"]),
        max_events_to_judge=int(sim["max_events_to_judge"]),
        rank_coverage_weight=float(sim["ranking"]["coverage_weight"]),
        rank_p2_weight=float(sim["ranking"]["p2_weight"]),
        strip_words=tuple(str(w) for w in sim["strip_words"]),
    )


def load_tuning(path: Path | None = None) -> Tuning:
    """Read on every call (cheap); an absent file means defaults."""
    path = path or DEFAULT_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    return build_tuning(raw or {})
