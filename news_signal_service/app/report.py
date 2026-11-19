"""Per-seed report: for every seed event its score and the deduplicated news events (with the other outlets that carried each story).

Seed score = tanh(sum of that seed's event signals), where each event signal is direction x intensity after the confidence gate and the
"one event = one full signal" weighting. It is informational: the FINAL score comes from the shock-group aggregation (correlated seeds
are discounted there), so seed scores do not add up to it. `contribution` is what an event actually added to the final score.
"""
from __future__ import annotations

import math

from app.domain import ImpactJudgement, RelevantArticle, SeedEvent

# which judged items to show, by status: scored = moved the score; relevant = also real events judged zero for a stated reason;
# all = everything the LLM looked at (including "not about this seed" and commentary).
SCORED = {"scored"}
# Real-looking items that were judged and zeroed for a stated reason (the CSV's event_status / evidence columns say why). Left out:
# "not_about_seed" (adjacent topics, the bulk of the vetoes) and "commentary" - both only under include=all.
RELEVANT = SCORED | {"priced_in", "no_direction", "expectation", "unverified",
                     "scheduled_or_expected", "opinion_or_forecast", "wrap_or_preview", "official_remarks", "background", "market_move"}
INCLUDE_MODES = {"scored": SCORED, "relevant": RELEVANT, "all": None}


def build_seed_report(
    seeds: tuple[SeedEvent, ...],
    relevant: list[RelevantArticle],
    judgements: list[ImpactJudgement],
    agg_rows: list[dict],
    clusters_by_id: dict,
) -> list[dict]:
    rel_by_key = {(r.article.article_id, r.seed_id): r for r in relevant}
    row_by_key: dict[tuple[str, str], dict] = {}
    for row in agg_rows:
        for aid in row["article_ids"]:
            row_by_key[(aid, row["seed_id"])] = row

    per_seed: dict[str, list[dict]] = {}
    for j in judgements:
        r = rel_by_key.get((j.article_id, j.seed_id))
        if r is None:
            continue
        cluster = clusters_by_id.get(r.cluster_id)
        own = r.article.source
        members = list(cluster.members) if cluster else [r.article]
        outlets = sorted({m.source for m in members})
        others = [m for m in members if m.article_id != r.article.article_id]
        row = row_by_key.get((j.article_id, j.seed_id), {})
        lead = bool(row) and row["article_ids"][0] == j.article_id  # a merged row's contribution is reported once, on its lead article
        per_seed.setdefault(j.seed_id, []).append({
            "headline": r.article.title,
            "source": own,
            "url": r.article.url,
            "published_at": r.article.published_at.isoformat(),
            "status": j.details.get("status", "scored" if j.direction else "no_direction"),
            "direction": j.direction,
            "intensity": j.intensity,
            "confidence": j.confidence,
            "signal": round(j.direction * j.intensity, 4),
            "contribution": row.get("counted", 0.0) if lead else 0.0,
            "event_weight": row.get("event_weight", 1.0),
            "provenance": j.details.get("provenance", ""),
            "event_status": j.details.get("event_status", ""),
            "relation_confidence": j.details.get("relation_confidence", ""),
            "evidence": j.details.get("evidence", ""),
            "surprise": j.details.get("surprise", ""),
            "source_weight": j.details.get("source_weight", 1.0),
            "match_score": r.match_confidence,
            "event_headlines": len(members),
            "event_outlets": len(outlets),
            "other_outlets": [o for o in outlets if o != own][:12],
            "related": [{"headline": m.title, "source": m.source, "url": m.url} for m in others[:5]],
            "rationale": j.rationale.split("] ", 1)[-1],
            "_cluster": r.cluster_id,
        })

    report: list[dict] = []
    for seed in seeds:
        articles = per_seed.pop(seed.seed_id, [])
        rows = [r for r in agg_rows if r["seed_id"] == seed.seed_id]
        score = math.tanh(sum(r["signal"] for r in rows)) if rows else 0.0
        articles.sort(key=lambda a: (-abs(a["contribution"]), -abs(a["signal"]), -a["confidence"]))
        report.append(_entry(seed.seed_id, seed.family, seed.event_type, seed.shock_group, score, articles))
    for seed_id, articles in per_seed.items():  # e.g. U00, the unseeded catch-all
        rows = [r for r in agg_rows if r["seed_id"] == seed_id]
        score = math.tanh(sum(r["signal"] for r in rows)) if rows else 0.0
        report.append(_entry(seed_id, "Unseeded high-impact", "Unseeded high-impact shock", "UNSEEDED", score, articles))
    return report


def _entry(seed_id: str, family: str, event_type: str, group: str, score: float, articles: list[dict]) -> dict:
    scored = [a for a in articles if a["status"] == "scored"]
    clusters = {a["_cluster"] for a in articles}
    for a in articles:
        a.pop("_cluster", None)
    return {
        "seed_id": seed_id, "family": family, "event_type": event_type, "shock_group": group,
        "score": round(score, 4), "events_judged": len(clusters), "events_scored": len(scored), "articles": articles,
    }


def filter_report(seeds: list[dict], include: str) -> list[dict]:
    """The same report with each seed's articles limited to `include` (scored | relevant | all). Seeds themselves are always kept."""
    if include not in INCLUDE_MODES:
        raise ValueError(f"include must be one of {sorted(INCLUDE_MODES)}")
    allowed = INCLUDE_MODES[include]
    if allowed is None:
        return seeds
    return [{**s, "articles": [a for a in s["articles"] if a["status"] in allowed]} for s in seeds]
