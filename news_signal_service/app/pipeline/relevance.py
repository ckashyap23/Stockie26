"""Step 2c - decide which event clusters relate to a seeded event, with a similarity score.

With an embedder (the normal path) relevance is a SIMILARITY decision:

    similarity(headline, seed) = max over the seed's short texts of cosine(headline, text)
                                 texts = [event type] + ["event type: alias" for each alias]
    score = min(1, similarity + keyword_boost * keyword_evidence)         keyword evidence = min(1, kw/2)
    relevant to the seed  <=>  score >= gate

Keywords therefore only LIFT a score; they cannot make a headline relevant on their own. The gate, the keyword boost and the
seed-text recipe were chosen with `python -m app.eval.relevance_benchmark` (all-mpnet-base-v2 beat TF-IDF, MiniLM, bge-small and
FinBERT-base; FinBERT vectors are near chance for this). Without an embedder (unit tests / no models installed) the old
keyword gate is used. Headlines matching no seed but carrying enough high-impact words go to the "unseeded" catch-all.
Thresholds and weights: data/tuning.yaml (`similarity:`).
"""
from __future__ import annotations

import math
import re

from app.domain import Article, RelevantArticle, SeedEvent
from app.pipeline.company_gate import CompanyRules
from app.pipeline.dedupe import EventCluster
from app.pipeline.embedding import Embedder, Vector, cosine
from app.pipeline.exclusion import first_exclusion
from app.tuning import Tuning, load_tuning

try:  # numpy ships with sentence-transformers; without it the pure-python loop is used
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

_STOPWORDS = {"and", "or", "the", "of", "in", "on", "to", "for", "a", "an", "new", "change", "changes"}
_TRACE_FLOOR = 0.7  # trace rows whose score reaches this share of the gate (near misses stay visible in the run folder)

# Seed-text vectors never change between runs: embed each (model, text) once per process.
_SEED_VECTOR_CACHE: dict[tuple[str, str], Vector] = {}


def build_unseeded(tuning: Tuning | None = None) -> SeedEvent:
    t = tuning or load_tuning()
    return SeedEvent(
        seed_id="U00", family="Unseeded high-impact", geography="INTL", event_type="Unseeded high-impact shock",
        priority="P2", timing="Ad hoc", aliases=(), source_ids=(), hypothesis="Novel shock outside the seed list",
        guardrail="Low priority; review manually and promote to a real seed if it recurs",
        llm_guidance="A novel high-impact shock that matches no listed event type. Score it only if it is a clear, fresh event likely to move Indian equities at the next open; otherwise relates=false.",
        shock_group="UNSEEDED",
    )


UNSEEDED = build_unseeded()  # default-config instance (tests / callers without a Tuning)


def seed_parts(seed: SeedEvent, strip_words: tuple[str, ...] = ("NIFTY",)) -> list[str]:
    """The short texts a headline is compared with. Generic index words are stripped: 'NIFTY heavyweight earnings' otherwise
    attracts every 'Nifty ends lower' market wrap."""
    def clean(text: str) -> str:
        for word in strip_words:
            text = re.sub(rf"\b{re.escape(word)}\b\s*", "", text, flags=re.IGNORECASE)
        return text.strip()

    event = clean(seed.event_type)
    return [event, *[f"{event}: {alias}" for alias in seed.aliases]]


def _terms(seed: SeedEvent) -> list[str]:
    terms = [alias.lower() for alias in seed.aliases]
    terms += [w for w in re.findall(r"[a-z]{4,}", seed.event_type.lower()) if w not in _STOPWORDS]
    return list(dict.fromkeys(terms))


def _hit(text: str, term: str) -> bool:
    if " " in term or "-" in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def _keyword_scores(article: Article, seed_terms, tuning: Tuning) -> dict[str, tuple[float, tuple[str, ...]]]:
    """Alias hit in the headline scores more than one only in the summary (a stray summary word must not match)."""
    title = article.title.lower()
    full = f"{title} {article.summary.lower()}"
    scored: dict[str, tuple[float, tuple[str, ...]]] = {}
    for seed, terms in seed_terms:
        if seed.context_terms and not any(_hit(full, c) for c in seed.context_terms):
            continue
        aliases = terms[: len(seed.aliases)]
        words = terms[len(seed.aliases):]
        alias_hits = [
            (t, tuning.alias_in_title if _hit(title, t) else tuning.alias_in_summary_only) for t in aliases if _hit(full, t)
        ]
        word_hits = [t for t in words if _hit(full, t)]
        score = sum(w for _, w in alias_hits) + tuning.event_type_word * len(word_hits)
        if score > 0:
            scored[seed.seed_id] = (score, tuple([t for t, _ in alias_hits] + word_hits))
    return scored


def similarity_matrix(
    articles: list[Article], seeds: tuple[SeedEvent, ...], embedder: Embedder, strip_words: tuple[str, ...],
) -> list[list[float]]:
    """sim[article][seed] = max over the seed's texts of cosine(headline, text), clipped at 0."""
    parts = [seed_parts(seed, strip_words) for seed in seeds]
    if embedder.kind == "sparse":
        # TF-IDF weights depend on the corpus, so seed texts and headlines must be embedded together (and are not cached).
        flat = [t for group in parts for t in group]
        vectors = embedder.embed(flat + [a.title for a in articles])
        part_vecs, headline_vectors = vectors[: len(flat)], vectors[len(flat):]
        out, k = [], 0
        owned = []
        for group in parts:
            owned.append(part_vecs[k:k + len(group)])
            k += len(group)
        return [[max(0.0, max(cosine(h, v) for v in group)) for group in owned] for h in headline_vectors]
    missing = sorted({t for group in parts for t in group if (embedder.name, t) not in _SEED_VECTOR_CACHE})
    if missing:
        for text, vec in zip(missing, embedder.embed(missing)):
            _SEED_VECTOR_CACHE[(embedder.name, text)] = vec
    headline_vectors = embedder.embed([a.title for a in articles]) if articles else []
    if _np is not None and headline_vectors:
        flat = [_SEED_VECTOR_CACHE[(embedder.name, t)] for group in parts for t in group]
        offsets, total = [], 0
        for group in parts:
            offsets.append(total)
            total += len(group)
        scores = _np.asarray(headline_vectors, dtype=_np.float32) @ _np.asarray(flat, dtype=_np.float32).T
        best = _np.maximum.reduceat(scores, _np.asarray(offsets), axis=1)  # max over each seed's texts
        return _np.clip(best, 0.0, None).tolist()
    return [
        [max(0.0, max(cosine(h, _SEED_VECTOR_CACHE[(embedder.name, t)]) for t in group)) for group in parts]
        for h in headline_vectors
    ]


def match_articles(
    articles: list[Article],
    seeds: tuple[SeedEvent, ...],
    min_score: float = 1.0,
    max_seeds_per_article: int | None = None,
    embedder: Embedder | None = None,
    gate: float | None = None,
    unseeded_catch_all: bool = False,
    clusters: dict[str, EventCluster] | None = None,
    trace: list[dict] | None = None,
    company_rules: CompanyRules | None = None,
    tuning: Tuning | None = None,
) -> list[RelevantArticle]:
    """`articles` should be cluster representatives; `clusters` (article_id -> cluster) adds size/sources.

    `min_score` is used only on the keyword-only path (no embedder); `gate` defaults to the tuning value for the embedder kind.
    """
    tuning = tuning or load_tuning()
    max_seeds = max_seeds_per_article or tuning.max_seeds_per_article
    unseeded = build_unseeded(tuning)
    seed_terms = [(seed, _terms(seed)) for seed in seeds]
    if embedder is not None:
        gate = gate if gate is not None else tuning.relevance_gate(embedder.kind)
        sims = similarity_matrix(articles, seeds, embedder, tuning.strip_words) if articles else []
    else:
        sims = []

    out: list[RelevantArticle] = []
    for idx, article in enumerate(articles):
        kw = _keyword_scores(article, seed_terms, tuning)
        candidates: list[tuple[float, float, SeedEvent, tuple[str, ...]]] = []  # (score, kw score, seed, hits)
        named_by_seed: dict[str, list] = {}
        for s_idx, (seed, _) in enumerate(seed_terms):
            kw_score, hits = kw.get(seed.seed_id, (0.0, ()))
            kw_norm = min(1.0, kw_score / 2.0)
            sim = sims[idx][s_idx] if embedder is not None else 0.0
            if embedder is not None:
                score = min(1.0, sim + tuning.keyword_boost * kw_norm)
                passed = score >= gate
            else:
                score = kw_norm
                passed = kw_score >= min_score
            reason = ""
            if seed.context_terms and kw_score == 0:
                reason = "context_terms_missing"  # similarity alone cannot satisfy a context requirement
            elif not passed:
                reason = "below_gate"
            elif seed.exclude_cues and (hit := first_exclusion(article.title, seed.exclude_cues)):
                reason = f"excluded_by_cue:{hit}"  # a filter: the cue never admits anything, it only removes
            elif company_rules is not None and company_rules.applies_to(seed.seed_id):
                named = company_rules.named(article.title)  # headline only: roundups list names in the summary
                if not named:
                    reason = "no_heavyweight_named"
                else:
                    named_by_seed[seed.seed_id] = named
            if trace is not None and (kw_score > 0 or (embedder is not None and score >= _TRACE_FLOOR * (gate or 0))):
                trace.append({
                    "article_id": article.article_id, "seed_id": seed.seed_id, "keyword_score": round(kw_score, 3),
                    "terms": list(hits), "similarity": round(sim, 4), "keyword_norm": round(kw_norm, 3),
                    "confidence": round(score, 4), "gate": gate, "passed_thresholds": not reason,
                    "reject_reason": reason, "selected": False,
                    **({"heavyweights": [h.name for h in named_by_seed[seed.seed_id]]} if seed.seed_id in named_by_seed else {}),
                })
            if reason:
                continue
            candidates.append((score, kw_score, seed, hits))
        candidates.sort(key=lambda c: c[0], reverse=True)

        if not candidates and unseeded_catch_all:
            text = f"{article.title} {article.summary}".lower()
            hits = tuple(w for w in tuning.unseeded_words if _hit(text, w))
            if len(hits) >= tuning.unseeded_min_hits:
                candidates = [(tuning.unseeded_confidence, float(len(hits)), unseeded, hits)]

        cluster = (clusters or {}).get(article.article_id)
        for score, kw_score, seed, hits in candidates[:max_seeds]:
            if trace is not None:
                row = next((t for t in reversed(trace) if t["article_id"] == article.article_id and t["seed_id"] == seed.seed_id), None)
                if row is None:  # unseeded catch-all has no scored row yet
                    row = {"article_id": article.article_id, "seed_id": seed.seed_id, "keyword_score": kw_score,
                           "terms": list(hits), "similarity": 0.0, "keyword_norm": 0.0, "confidence": round(score, 4),
                           "gate": gate, "passed_thresholds": True, "reject_reason": "", "selected": False}
                    trace.append(row)
                row["selected"] = True
            out.append(RelevantArticle(
                article, seed.seed_id, seed.family, seed.geography, kw_score, hits,
                match_confidence=round(score, 4),
                cluster_id=cluster.cluster_id if cluster else f"c-{article.article_id}",
                cluster_size=len(cluster.members) if cluster else 1,
                cluster_sources=cluster.sources if cluster else 1,
                heavyweights=tuple(h.name for h in named_by_seed.get(seed.seed_id, ())),
                weight_scale=company_rules.weight_scale(named_by_seed[seed.seed_id])
                if company_rules is not None and seed.seed_id in named_by_seed else 1.0,
            ))
    return out


def consolidate_clusters(links: list[RelevantArticle], max_seeds: int, source_quality=None) -> list[RelevantArticle]:
    """Seed matching runs on EVERY headline of a cluster (a wrong merge must not hide an event). Here each cluster keeps, per seed, only
    its best headline (similarity x source quality, so a Facebook repost does not beat a wire story), and then only its `max_seeds`
    strongest seeds, so one event is judged once per seed."""
    def key(link: RelevantArticle) -> float:
        return link.match_confidence * (source_quality.weight(link.article.source) if source_quality else 1.0)

    best: dict[tuple[str, str], RelevantArticle] = {}
    for link in links:
        k = (link.cluster_id, link.seed_id)
        if k not in best or key(link) > key(best[k]):
            best[k] = link
    per_cluster: dict[str, list[RelevantArticle]] = {}
    for link in best.values():
        per_cluster.setdefault(link.cluster_id, []).append(link)
    out: list[RelevantArticle] = []
    for cluster_links in per_cluster.values():
        cluster_links.sort(key=lambda l: l.match_confidence, reverse=True)
        out.extend(cluster_links[:max_seeds])
    return out


def event_importance(link: RelevantArticle, priority: str, tuning: Tuning) -> float:
    """How much an event deserves one of the limited judging slots: similarity, lifted by how widely it is covered (distinct outlets),
    and trimmed for P2 seeds. A 17-outlet story outranks a lone outlet with slightly higher similarity."""
    coverage = 1.0 + tuning.rank_coverage_weight * math.log(max(1, link.cluster_sources))
    return link.match_confidence * coverage * (tuning.rank_p2_weight if priority == "P2" else 1.0)


def select_top_events(
    links: list[RelevantArticle], seeds: dict[str, SeedEvent], tuning: Tuning,
) -> tuple[list[RelevantArticle], int]:
    """Keep the `max_events_to_judge` most important events (clusters). Returns (kept links, events cut)."""
    importance: dict[str, float] = {}
    for link in links:
        seed = seeds.get(link.seed_id)
        score = event_importance(link, seed.priority if seed else "P2", tuning)
        importance[link.cluster_id] = max(importance.get(link.cluster_id, 0.0), score)
    if len(importance) <= tuning.max_events_to_judge:
        return links, 0
    keep = {cid for cid, _ in sorted(importance.items(), key=lambda kv: kv[1], reverse=True)[: tuning.max_events_to_judge]}
    return [l for l in links if l.cluster_id in keep], len(importance) - len(keep)
