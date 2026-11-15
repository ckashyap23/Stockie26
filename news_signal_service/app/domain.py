"""Core data types shared by every pipeline stage."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SeedEvent:
    """One entry of the pre-seeded macro event list (data/event_seeds.csv)."""

    seed_id: str
    family: str
    geography: str  # Domestic | INTL
    event_type: str
    priority: str  # P1 | P2
    timing: str  # Scheduled | Ad hoc | Scheduled/ad hoc
    aliases: tuple[str, ...]
    source_ids: tuple[str, ...]
    hypothesis: str
    guardrail: str
    llm_guidance: str = ""  # per-seed note appended to the LLM prompt
    shock_group: str = ""  # seeds in one group are observations of the same shock
    exclude_cues: tuple[str, ...] = ()  # headline phrases that mean "not a fresh event for this seed" (filter only)
    context_terms: tuple[str, ...] = ()  # if set, a match must also mention one of these (e.g. an India link)


@dataclass(frozen=True)
class NewsSource:
    """One publisher from data/feeds.yaml."""

    source_id: str
    publisher: str
    geography: str
    priority: str
    access: str
    body: str  # full | headline
    enabled: bool
    rights: str
    notes: str
    feeds: tuple[str, ...]


@dataclass(frozen=True)
class Article:
    article_id: str
    source: str
    url: str
    title: str
    summary: str
    published_at: datetime  # first publication time as first seen (never moves later)
    fetched_at: datetime  # first ingestion time
    region: str
    provider: str
    content: str = ""  # full text once extracted
    content_source: str = "summary"  # summary | full_text | headline_only
    updated_at: datetime | None = None  # later feed timestamp, if the publisher revised the item
    source_id: str = ""
    body_mode: str = "full"  # full | headline (paywalled: never fetch the body)
    source_priority: str = "P2"  # P1 sources win when choosing a duplicate cluster's representative

    def text_for_model(self) -> str:
        body = self.content or self.summary
        return "\n\n".join(part.strip() for part in (self.title, body) if part and part.strip())

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("published_at", "fetched_at", "updated_at"):
            row[key] = row[key].isoformat() if row[key] else None
        return row


@dataclass(frozen=True)
class RelevantArticle:
    """An article (one per deduplicated event cluster) matched to a seed event."""

    article: Article
    seed_id: str
    family: str
    geography: str
    relevance: float  # raw keyword score
    matched_terms: tuple[str, ...]
    match_confidence: float = 1.0  # 0..1 confidence that the headline belongs to this seed
    cluster_id: str = ""
    cluster_size: int = 1  # articles describing the same event
    cluster_sources: int = 1  # distinct publishers in the cluster
    heavyweights: tuple[str, ...] = ()  # NIFTY heavyweights named (company-level seeds only)
    weight_scale: float = 1.0  # index-weight scaling applied to intensity (company-level seeds only)


@dataclass(frozen=True)
class ImpactJudgement:
    """Impact of one event cluster on NIFTY (pipeline step 4).

    confidence = support for the interpretation; intensity = hypothesised economic size.
    They are kept separate and neither is a calibrated probability or a predicted % move.
    """

    article_id: str
    seed_id: str
    family: str
    direction: int  # +1 / 0 / -1
    confidence: float  # 0..1
    intensity: float  # 0..1
    rationale: str
    model_name: str
    shock_group: str = ""
    cluster_size: int = 1
    cluster_sources: int = 1
    heavyweights: tuple[str, ...] = ()
    weight_scale: float = 1.0
    cluster_id: str = ""  # the event (dedupe cluster) this judgement is about: one event = one full signal
    details: dict = field(default_factory=dict)  # LLM audit data: status, relates, surprise, cached ...


@dataclass
class FamilySignal:
    family: str
    score: float  # signed, -1..1
    cluster_count: int
    article_count: int
    top_events: list[str] = field(default_factory=list)


@dataclass
class GroupSignal:
    """One underlying shock (several seeds can be channels of it)."""

    group: str
    score: float  # signed, -1..1, after correlated-channel discounting
    raw_sum: float  # what a naive sum of the channels would have given
    seeds: list[str] = field(default_factory=list)


@dataclass
class SignalResult:
    """Final output of the pipeline (pipeline step 5)."""

    target_date: str
    generated_at: str
    window_start: str
    window_end: str
    score: float  # signed -1..1 news score; sign = indicated NIFTY direction
    magnitude: float  # 0..1; see magnitude_basis
    direction: str  # positive | negative | neutral
    confidence: float  # 0..1 support for the interpretation; NOT a probability, separate from magnitude
    families: list[FamilySignal]
    groups: list[GroupSignal]
    article_count_fetched: int
    article_count_relevant: int
    cluster_count: int
    stage_status: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    calibrated: bool = False
    magnitude_basis: str = "uncalibrated_score"  # uncalibrated_score | calibrated_return
    predicted_return: float | None = None  # fraction, prev-close -> close; only when calibrated
    target_definition: str = "previous close -> close return of target_date (uncalibrated)"
    as_of: str = ""  # cutoff the evidence was restricted to
    evidence: list[dict[str, Any]] = field(default_factory=list)  # judged event clusters, strongest first
    seeds: list[dict[str, Any]] = field(default_factory=list)  # every seed: score + its deduplicated news events (app/report.py)
    judge_summary: dict[str, Any] = field(default_factory=dict)  # rules/llm counts, LLM calls, vetoes, unscored, errors
    run_id: str = ""  # <date>/<HHMMSS>: folder under DATA_DIR/runs holding every stage's artifacts

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
