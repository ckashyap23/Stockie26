"""Wires the stages: seeds -> headlines -> dedupe -> seed match -> full text -> FinBERT judge -> aggregate."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.calibration.model import Calibration
from app.config import Settings
from app.domain import SignalResult
from app.pipeline.aggregate import aggregate
from app.report import build_seed_report
from app.pipeline.commentary import CommentaryFilter
from app.pipeline.company_gate import CompanyRules
from app.pipeline.dedupe import cluster_articles, fair_cap
from app.pipeline.embedding import build_embedder
from app.pipeline.extract import extract_full_text
from app.pipeline.ingest import fetch_headlines, load_feeds
from app.pipeline.relevance import build_unseeded, consolidate_clusters, match_articles, select_top_events
from app.pipeline.source_quality import SourceQuality
from app.pipeline.seeds import load_seeds
from app.pipeline.llm_judge import LlmJudge
from app.llm.cache import LlmCache
from app.llm.client import build_llm_client
from app.storage.archive import ArticleArchive
from app.storage.run_store import RunRecorder
from app.tuning import load_tuning

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def news_window(target: date, settings: Settings) -> tuple[datetime, datetime]:
    """Previous session close -> cutoff on the target day (all IST).

    The previous session is the previous weekday, so a Monday window starts Friday 15:30 and covers
    the weekend. Exchange holidays are not modelled (a day after a weekday holiday starts one session late).
    """
    prev = target - timedelta(days=1)
    if settings.window_previous_day != "calendar_day":
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
    start = datetime.combine(prev, settings.window_start_time, tzinfo=IST)
    end = datetime.combine(target, settings.window_end_time, tzinfo=IST)
    return start, end


def run_signal(
    target: date, settings: Settings, replay: bool = False, record: bool = False, cutoff: datetime | None = None,
) -> SignalResult:
    """Compute the signal for `target`.

    replay=False: fetch live feeds, record them in the archive, use items first published by the cutoff.
    replay=True : do not fetch; rebuild from the archive exactly as it stood at the cutoff (backtests).
    record=True : write every stage's artifacts to DATA_DIR/runs/<date>/<HHMMSS>/ (see storage/run_store.py).
    cutoff      : optional override of the window end (IST); default is WINDOW_END_TIME on the target date.
    """
    status: dict[str, str] = {}
    warnings: list[str] = []
    rec = RunRecorder(settings.data_dir, target) if record else None
    mark = time.monotonic()

    def tick(stage: str) -> None:
        nonlocal mark
        now = time.monotonic()
        if rec:
            rec.timings[stage] = round(now - mark, 2)
        mark = now

    window_start, default_cutoff = news_window(target, settings)
    cutoff = cutoff or default_cutoff
    archive = ArticleArchive(settings.archive_file())

    seeds = load_seeds(settings.seeds_path)
    tuning = load_tuning(settings.tuning_path)
    status["1_seeds"] = f"ok ({len(seeds)} events)"
    tick("1_seeds")

    # --- 2. headlines, timestamped versions, cutoff ---------------------------------------------
    if replay:
        articles = archive.as_of(window_start, cutoff, settings.ingest_grace_minutes)
        status["2a_headlines"] = f"replay from archive ({len(articles)} knowable at cutoff)"
    else:
        feeds = load_feeds(settings.feeds_path)
        fetched = fetch_headlines(feeds, window_start, cutoff, settings.newsapi_key, settings.http_timeout_seconds)
        canonical = archive.record(fetched)
        # The window is read from the ARCHIVE: it holds what the ingest job collected through the night plus what was just fetched,
        # so a feed that only keeps its latest 50 items cannot make the window thin.
        articles = archive.in_window(window_start, cutoff)
        late = sum(1 for a in canonical if a.published_at.astimezone(IST) > cutoff)
        status["2a_headlines"] = (f"ok ({len(feeds)} feeds, {len(fetched)} fetched now, {len(articles)} in the "
                                  f"{window_start:%d %b %H:%M}-{cutoff:%d %b %H:%M} IST window incl. earlier ingests)")
        if late:
            warnings.append(f"{late} item(s) first published after the {cutoff:%H:%M} IST cutoff were excluded.")
        if not fetched:
            warnings.append("No articles fetched - check feeds, NEWSAPI_KEY and that the target date is current.")
    articles, cut = fair_cap(articles, settings.max_articles_per_run)
    if cut:
        warnings.append(f"{cut} headline(s) over the safety cap of {settings.max_articles_per_run} were left out, spread evenly over days and sources.")
    revised = sum(1 for a in articles if a.updated_at)
    if revised:
        warnings.append(f"{revised} item(s) were revised by the publisher; first-seen timestamps are used.")
    tick("2a_headlines")
    if rec:
        rec.write_jsonl("headlines.jsonl", ({k: v for k, v in a.to_dict().items() if k != "content"} for a in articles))

    # --- 2b. dedupe -> 2c. seed match with confidence ---------------------------------------------
    embedder = build_embedder(settings)
    dedupe_threshold = settings.dedupe_threshold if settings.dedupe_threshold is not None else tuning.dedupe_threshold(embedder.kind)
    gate = settings.relevance_gate if settings.relevance_gate is not None else tuning.relevance_gate(embedder.kind)
    if getattr(embedder, "fallback_reason", ""):
        warnings.append(f"Dense embedder unavailable ({getattr(embedder, 'fallback_reason', '')}); using the weaker TF-IDF fallback.")
    # Non-events (history, explainers, opinion), broker calls and stock-pick lists are removed from the individual headlines BEFORE
    # dedupe, so they cannot become the representative of (or absorb) a cluster of real news. Logged, never scored.
    commentary = CommentaryFilter.load(settings.commentary_path)
    articles, dropped = commentary.split(articles)
    clusters = cluster_articles(articles, embedder, dedupe_threshold)
    by_rep = {c.representative.article_id: c for c in clusters}
    by_member = {m.article_id: c for c in clusters for m in c.members}
    members = [m for c in clusters for m in c.members]
    tick("2b_dedupe")
    match_trace: list[dict] = []
    company_rules = CompanyRules.load(settings.heavyweights_path)
    source_quality = SourceQuality.load(settings.source_quality_path)
    # Match every headline (not just each cluster's representative), then keep each cluster's best link per seed.
    relevant = consolidate_clusters(match_articles(
        members, seeds,
        embedder=embedder, gate=gate,
        unseeded_catch_all=settings.unseeded_catch_all, clusters=by_member, trace=match_trace,
        company_rules=company_rules, tuning=tuning,
    ), tuning.max_seeds_per_article, source_quality)
    # Judge at most N events, the most IMPORTANT first (similarity x how many outlets cover it x seed priority): bounds LLM calls.
    relevant, capped = select_top_events(relevant, {s.seed_id: s for s in seeds}, tuning)
    if capped:
        warnings.append(f"{capped} lower-importance event(s) not judged (cap of {tuning.max_events_to_judge} per run).")
    matched_clusters = len({r.cluster_id for r in relevant})
    status["2b_dedupe"] = f"ok ({len(articles)} headlines -> {len(clusters)} events, embedder={embedder.name})"
    tick("2c_relevance")
    if rec:
        would_match: dict[str, list[str]] = {}
        if dropped:  # what the filter cost: the seeds each dropped item would have matched
            for m in match_articles([a for a, _ in dropped], seeds, embedder=embedder, gate=gate,
                                    company_rules=company_rules, tuning=tuning):
                would_match.setdefault(m.article.article_id, []).append(m.seed_id)
        rec.write_jsonl("commentary_dropped.jsonl", (
            {"article_id": a.article_id, "headline": a.title, "source": a.source, "url": a.url,
             "category": hit.category, "pattern": hit.pattern, "first_published": a.published_at.isoformat(),
             "would_have_matched": would_match.get(a.article_id, [])} for a, hit in dropped))
        rec.write_jsonl("clusters.jsonl", (
            {"cluster_id": c.cluster_id, "representative": c.representative.article_id, "headline": c.representative.title,
             "members": [m.article_id for m in c.members], "sources": sorted({m.source for m in c.members})}
            for c in clusters))
        rec.write_jsonl("matches.jsonl", match_trace)
    status["2c_relevance"] = (
        f"ok ({matched_clusters} of {len(clusters)} events matched a seed; {len(relevant)} links; "
        f"{len(dropped)} non-event/broker headlines filtered out)"
    )

    # --- 3. full text for relevant clusters only ----------------------------------------------------
    relevant, extract_status = extract_full_text(
        relevant,
        cache_dir=settings.data_dir / "cache",
        max_fetches=settings.fulltext_max_fetches,
        min_chars=settings.fulltext_min_chars,
        timeout=settings.http_timeout_seconds,
        domain_delay=settings.fulltext_domain_delay_seconds,
        enabled=settings.enable_fulltext and not replay,
    )
    status["3_full_text"] = extract_status
    tick("3_full_text")
    if rec:
        rec.write_jsonl("fulltext.jsonl", (
            {"article_id": r.article.article_id, "seed_id": r.seed_id, "url": r.article.url,
             "content_source": r.article.content_source, "chars": len(r.article.content)} for r in relevant))

    # --- 4. judge: the LLM decides relation, direction, size and confidence for every matched event -------------------
    client, llm_note = build_llm_client(settings)
    judge = LlmJudge(client, LlmCache(settings.data_dir / "cache" / "llm_judgements.sqlite3"), (*seeds, build_unseeded(tuning)),
                     tuning, max_calls=settings.llm_max_calls_per_run, not_configured_reason=llm_note, source_quality=source_quality)
    judgements, llm_report = judge.judge(relevant) if relevant else ([], None)
    tick("4_impact")
    status["4_impact"] = f"ok ({len(judgements)} events to the LLM"
    if llm_report:
        status["4_impact"] += f" [{llm_note if client else 'LLM OFF'}: {llm_report.summary()}]"
    status["4_impact"] += ")"
    judge_summary = {
        "llm": len(judgements), "llm_model": llm_note if client else "",
        "llm_calls": llm_report.calls if llm_report else 0, "llm_judged": llm_report.judged if llm_report else 0,
        "llm_cached": llm_report.cached if llm_report else 0, "llm_vetoed": llm_report.vetoed if llm_report else 0,
        "llm_unscored": llm_report.unscored if llm_report else 0,
        "llm_errors": list(dict.fromkeys(llm_report.errors))[:5] if llm_report else [],
    }
    if llm_report and llm_report.unscored:
        warnings.append(f"{llm_report.unscored} event(s) were NOT scored by the LLM: "
                        f"{'; '.join(dict.fromkeys(llm_report.errors))[:300] or 'unknown reason'}")
    if rec:
        rec.write_jsonl("judgements.jsonl", (asdict(j) for j in judgements))
        if llm_report and llm_report.call_log:
            rec.write_jsonl("llm_calls.jsonl", llm_report.call_log)

    # --- 5. aggregate (correlated channels discounted) --------------------------------------------------
    by_id = {r.article.article_id: r.article for r in relevant}
    agg = aggregate(
        judgements, by_id, scale=settings.aggregate_scale, neutral_band=settings.neutral_band,
        min_confidence=settings.min_confidence, min_signal=settings.min_signal, group_decay=settings.group_decay,
        event_secondary_weight=settings.event_secondary_weight,
    )
    tick("5_aggregate")
    if rec:
        rec.write_json("aggregation.json", {
            "clusters": agg.clusters, "groups": [asdict(g) for g in agg.groups], "families": [asdict(f) for f in agg.families],
            "params": {"scale": settings.aggregate_scale, "min_confidence": settings.min_confidence,
                       "min_signal": settings.min_signal, "group_decay": settings.group_decay},
            "score": agg.score, "confidence": agg.confidence})
    status["5_aggregate"] = f"ok ({agg.cluster_count} clusters, {len(agg.groups)} active shock groups)"

    # --- calibration (optional) -----------------------------------------------------------------------
    result = SignalResult(
        target_date=target.isoformat(),
        generated_at=datetime.now(IST).isoformat(),
        window_start=window_start.isoformat(),
        window_end=cutoff.isoformat(),
        score=agg.score,
        magnitude=agg.magnitude,
        direction=agg.direction,
        confidence=agg.confidence,
        families=agg.families,
        groups=agg.groups,
        article_count_fetched=len(articles),
        article_count_relevant=len({r.article.article_id for r in relevant}),
        cluster_count=agg.cluster_count,
        stage_status=status,
        warnings=warnings,
        as_of=cutoff.isoformat(),
        judge_summary=judge_summary,
        evidence=_evidence(judgements, by_id, relevant),
        seeds=build_seed_report(seeds, relevant, judgements, agg.clusters, {c.cluster_id: c for c in clusters}),
    )
    calibration = Calibration.load(settings.calibration_file())
    if calibration:
        predicted = calibration.predicted_return(agg.score)
        result.predicted_return = round(predicted, 6)
        result.magnitude = round(calibration.magnitude(predicted), 4)
        result.magnitude_basis = "calibrated_return"
        result.calibrated = True
        result.target_definition = calibration.target
        status["6_calibration"] = f"applied (n={calibration.n_train}, through {calibration.fitted_through})"
    else:
        status["6_calibration"] = "none: score and magnitude are uncalibrated"
        warnings.append("Uncalibrated: magnitude = |score|, not an expected move. Run app.calibration.fit once enough history exists.")
    tick("6_calibration")
    if rec:
        result.run_id = rec.run_id
        rec.write_json("signal.json", result.to_dict())
        rec.write_meta(settings, replay=replay, window=(window_start.isoformat(), cutoff.isoformat()),
                       warnings=warnings, status=status)
    return result


def _evidence(judgements, articles_by_id, relevant) -> list[dict]:
    meta = {(r.article.article_id, r.seed_id): r for r in relevant}
    rows = []
    for j in judgements:
        r = meta[(j.article_id, j.seed_id)]
        a = articles_by_id[j.article_id]
        rows.append({
            "headline": a.title, "source": a.source, "url": a.url, "seed_id": j.seed_id, "family": j.family,
            "shock_group": j.shock_group, "direction": j.direction, "confidence": j.confidence,
            "intensity": j.intensity, "signal": round(j.direction * j.intensity, 4),
            "match_confidence": r.match_confidence, "heavyweights": list(r.heavyweights), "weight_scale": r.weight_scale, "cluster_size": r.cluster_size,
            "cluster_sources": r.cluster_sources, "content_source": a.content_source,
            "first_published": a.published_at.isoformat(), "rationale": j.rationale, "details": j.details,
        })
    rows.sort(key=lambda row: abs(row["signal"]), reverse=True)
    return rows[:50]
