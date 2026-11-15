from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import SERVICE_ROOT
from app.domain import Article, ImpactJudgement
from app.pipeline.aggregate import aggregate, cluster_judgements
from app.pipeline.relevance import match_articles
from app.pipeline.seeds import load_seeds, load_sources

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 1, 5, 8, 0, tzinfo=IST)
SEEDS_CSV = SERVICE_ROOT / "data" / "event_seeds.csv"


def _article(article_id: str, title: str, summary: str = "", **kw) -> Article:
    return Article(article_id, "Test", f"http://x/{article_id}", title, summary, NOW, NOW, "india", "rss", **kw)


def _judgement(article_id, seed, family, direction, conf=0.9, intensity=0.7, group="") -> ImpactJudgement:
    return ImpactJudgement(article_id, seed, family, direction, conf, intensity, "t", "test", shock_group=group)


def test_seed_and_source_catalogues_load():
    seeds = load_seeds(SEEDS_CSV)
    sources = load_sources(SERVICE_ROOT / "data" / "feeds.yaml")
    assert len(seeds) == 61 and len(sources) == 18 and all(s.feeds for s in sources)
    assert all(seed.aliases for seed in seeds)


def test_every_seed_has_a_shock_group_and_cues_are_only_exclusions():
    seeds = load_seeds(SEEDS_CSV)
    assert all(s.shock_group for s in seeds)
    assert all(not hasattr(s, "bullish_cues") and not hasattr(s, "bearish_cues") for s in seeds)  # direction cues are gone
    for s in seeds:
        assert not {c.lower() for c in s.exclude_cues} & {a.lower() for a in s.aliases}, s.seed_id


def test_relevance_matches_seed_alias_and_drops_noise():
    seeds = load_seeds(SEEDS_CSV)
    articles = [_article("a", "RBI MPC holds repo rate; hints at CRR change"), _article("b", "Celebrity wedding photos")]
    matched = match_articles(articles, seeds)
    assert {m.article.article_id for m in matched} == {"a"}
    assert 0 < matched[0].match_confidence <= 1


def test_duplicate_headlines_count_as_one_cluster():
    arts = {i: _article(i, "Trump pauses tariffs on India for 90 days") for i in ("a", "b", "c")}
    js = [_judgement(i, "T01", "Trade", 1) for i in arts]
    assert len(cluster_judgements(js, arts)) == 1


def test_aggregate_sign_follows_evidence_and_is_bounded():
    arts = {"a": _article("a", "Fed signals more rate cuts"), "b": _article("b", "Iran attack closes Hormuz shipping lanes")}
    pos = aggregate([_judgement("a", "M04", "Monetary", 1)], arts)
    neg = aggregate([_judgement("b", "E01", "Energy", -1)], arts)
    assert pos.score > 0 and pos.direction == "positive"
    assert neg.score < 0 and neg.direction == "negative"
    assert 0 <= pos.magnitude <= 1 and abs(neg.score) <= 1


def test_aggregate_empty_is_neutral():
    result = aggregate([], {})
    assert (result.score, result.magnitude, result.direction, result.cluster_count) == (0.0, 0.0, "neutral", 0)
    assert result.confidence == 0.0 and result.groups == []


def test_correlated_channels_are_discounted_not_summed():
    arts = {k: _article(k, f"headline {k} unique words {k}") for k in "abc"}
    one_shock = [
        _judgement("a", "G02", "Conflict", -1, group="OIL_GEO"),
        _judgement("b", "E02", "Energy", -1, group="OIL_GEO"),
        _judgement("c", "G04", "Conflict", -1, group="OIL_GEO"),
    ]
    separate = [
        _judgement("a", "G02", "Conflict", -1, group="X1"),
        _judgement("b", "E02", "Energy", -1, group="X2"),
        _judgement("c", "G04", "Conflict", -1, group="X3"),
    ]
    correlated, independent = aggregate(one_shock, arts), aggregate(separate, arts)
    assert abs(correlated.score) < abs(independent.score)
    assert correlated.groups[0].raw_sum < correlated.groups[0].score * 0 + -0.7 * 2  # naive sum would be -2.1


def test_confidence_gates_but_does_not_scale_magnitude():
    arts = {"a": _article("a", "x one"), "b": _article("b", "y two")}
    high = aggregate([_judgement("a", "M04", "Monetary", 1, conf=0.95)], arts, min_confidence=0.35)
    mid = aggregate([_judgement("b", "M04", "Monetary", 1, conf=0.5)], arts, min_confidence=0.35)
    low = aggregate([_judgement("b", "M04", "Monetary", 1, conf=0.2)], arts, min_confidence=0.35)
    assert high.score == mid.score > 0  # same intensity -> same size; confidence only reported
    assert high.confidence > mid.confidence
    assert low.score == 0.0 and low.direction == "neutral"  # below the gate -> exactly zero


def test_window_runs_from_previous_weekday_1600_to_0900():
    from datetime import date
    from app.config import Settings
    from app.pipeline.orchestrator import news_window

    s = Settings()
    tue = news_window(date(2026, 11, 10), s)  # Tuesday
    mon = news_window(date(2026, 11, 9), s)  # Monday: starts Friday, covers the weekend
    assert (tue[0].day, tue[0].hour, tue[0].minute, tue[1].day, tue[1].hour, tue[1].minute) == (9, 15, 30, 10, 8, 30)
    assert (mon[0].day, mon[0].hour, mon[1].hour, mon[1].minute) == (6, 15, 8, 30)
