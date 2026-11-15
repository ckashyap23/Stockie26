"""Step 5 - roll event-cluster judgements up into one signed news score.

Formula (all quantities are uncalibrated scores, not probabilities or % moves)
-------
1. Cluster near-duplicate judgements per seed event (a syndicated story is one event).
2. cluster_signal = direction * intensity. Confidence is a *gate*, not a multiplier: a cluster below
   `min_confidence`, or with |signal| below `min_signal`, contributes exactly zero. Confidence is
   reported separately.
3. Correlated channels: seeds sharing a `shock_group` (an attack, an oil jump and a global selloff
   are one shock). Within a group the clusters are ranked by |signal| and the k-th counts
   `decay**k`, so the strongest channel counts in full and echoes add less. Opposing channels net.
   group_score = tanh(discounted sum).
4. raw   = sum(group_weight * group_score)       (weights default 1.0; calibrate later)
5. score = tanh(raw / scale)                      in (-1, 1); sign = indicated NIFTY direction
   magnitude (uncalibrated) = |score|
The mapping from score to an expected return is a separate, calibrated step (app/calibration).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.domain import Article, FamilySignal, GroupSignal, ImpactJudgement

_TOKEN = re.compile(r"[a-z0-9]{3,}")


@dataclass
class AggregateResult:
    score: float = 0.0
    magnitude: float = 0.0
    direction: str = "neutral"
    confidence: float = 0.0
    families: list[FamilySignal] = field(default_factory=list)
    groups: list[GroupSignal] = field(default_factory=list)
    cluster_count: int = 0
    clusters: list[dict] = field(default_factory=list)  # per-cluster audit rows (incl. gated-out ones)


def _tokens(title: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(title.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def cluster_judgements(
    judgements: list[ImpactJudgement],
    articles_by_id: dict[str, Article],
    threshold: float = 0.6,
) -> list[list[ImpactJudgement]]:
    """Greedy title-similarity clustering within each seed event."""
    by_seed: dict[str, list[ImpactJudgement]] = defaultdict(list)
    for judgement in judgements:
        by_seed[judgement.seed_id].append(judgement)
    clusters: list[list[ImpactJudgement]] = []
    for group in by_seed.values():
        local: list[tuple[frozenset[str], list[ImpactJudgement]]] = []
        for judgement in group:
            tokens = _tokens(articles_by_id[judgement.article_id].title)
            for rep_tokens, members in local:
                if _jaccard(tokens, rep_tokens) >= threshold:
                    members.append(judgement)
                    break
            else:
                local.append((tokens, [judgement]))
        clusters.extend(members for _, members in local)
    return clusters


def cluster_signal(members: list[ImpactJudgement]) -> tuple[float, float]:
    """(signed signal = direction * intensity, mean confidence). Confidence is NOT folded into the signal."""
    weight = sum(m.confidence for m in members)
    if weight <= 0:
        return 0.0, 0.0
    direction = sum(m.direction * m.confidence for m in members) / weight
    intensity = max(m.intensity for m in members)
    return direction * intensity, weight / len(members)


def aggregate(
    judgements: list[ImpactJudgement],
    articles_by_id: dict[str, Article],
    group_weights: dict[str, float] | None = None,
    scale: float = 3.0,
    neutral_band: float = 0.05,
    min_confidence: float = 0.0,
    min_signal: float = 0.0,
    group_decay: float = 0.5,
    event_secondary_weight: float = 1.0,
) -> AggregateResult:
    weights = group_weights or {}
    clusters = cluster_judgements(judgements, articles_by_id)

    # one audit row per cluster, including ones the gate zeroed
    rows: list[dict] = []
    for members in clusters:
        raw_signal, confidence = cluster_signal(members)
        gated = confidence < min_confidence or abs(raw_signal) < min_signal
        rows.append({
            "seed_id": members[0].seed_id, "family": members[0].family,
            "group": members[0].shock_group or members[0].seed_id,
            "article_ids": [m.article_id for m in members],
            "raw_signal": round(raw_signal, 4), "confidence": round(confidence, 4),
            "gated_to_zero": bool(raw_signal) and gated,
            "signal": 0.0 if gated else raw_signal,
            "event_id": members[0].cluster_id, "event_weight": 1.0,
            "rank_in_group": None, "decay_weight": None, "counted": 0.0,
        })

    # One event = one full signal. An event (dedupe cluster) judged under several seeds counts its strongest seed in full and each
    # further seed at `event_secondary_weight` per rank (the same story must not enter twice through different shock groups).
    by_event: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["signal"] and row["event_id"]:
            by_event[row["event_id"]].append(row)
    for event_rows in by_event.values():
        for rank, row in enumerate(sorted(event_rows, key=lambda r: abs(r["signal"]), reverse=True)):
            row["event_weight"] = event_secondary_weight ** rank
            row["signal"] *= row["event_weight"]

    by_group: dict[str, list[dict]] = defaultdict(list)
    by_family: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for row in rows:
        if row["signal"]:
            by_group[row["group"]].append(row)
            by_family[row["family"]].append((row["signal"], row["seed_id"]))

    groups: list[GroupSignal] = []
    raw = 0.0
    for group, items in by_group.items():
        ranked = sorted(items, key=lambda r: abs(r["signal"]), reverse=True)
        for k, row in enumerate(ranked):
            row["rank_in_group"], row["decay_weight"] = k, group_decay ** k
            row["counted"] = round(row["signal"] * group_decay ** k, 4)
        group_score = math.tanh(sum(r["signal"] * r["decay_weight"] for r in ranked))
        raw += weights.get(group, 1.0) * group_score
        groups.append(GroupSignal(group, round(group_score, 4), round(sum(r["signal"] for r in ranked), 4), [r["seed_id"] for r in ranked]))
    groups.sort(key=lambda g: abs(g.score), reverse=True)

    families = [
        FamilySignal(
            family, round(math.tanh(sum(s for s, _ in items)), 4), len(items), len(items),
            [seed for _, seed in sorted(items, key=lambda it: abs(it[0]), reverse=True)[:3]],
        )
        for family, items in by_family.items()
    ]
    families.sort(key=lambda f: abs(f.score), reverse=True)

    score = math.tanh(raw / scale) if scale else 0.0
    direction = "neutral" if abs(score) < neutral_band else ("positive" if score > 0 else "negative")
    live = [(abs(r["signal"]), r["confidence"]) for r in rows if r["signal"]]
    total = sum(w for w, _ in live)
    confidence = sum(w * c for w, c in live) / total if total else 0.0
    return AggregateResult(
        round(score, 4), round(abs(score), 4), direction, round(confidence, 4), families, groups, len(clusters), rows,
    )
