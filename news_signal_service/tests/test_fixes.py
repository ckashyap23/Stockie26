"""Tests for: importance ranking, one-event-one-signal, source quality, freshness + provenance, heavyweight corporate events."""
import json
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import CONFIG_FILES, PACKAGED_CONFIG_DIR, SERVICE_ROOT, Settings
from app.domain import Article, ImpactJudgement, RelevantArticle
from app.llm.cache import LlmCache
from app.llm.client import LlmResponse
from app.llm.prompt import PROMPT_VERSION, SYSTEM_PROMPT, parse_response
from app.pipeline.aggregate import aggregate
from app.pipeline.company_gate import CompanyRules
from app.pipeline.llm_judge import LlmJudge
from app.pipeline.relevance import consolidate_clusters, event_importance, match_articles, select_top_events
from app.pipeline.seeds import load_seeds
from app.pipeline.source_quality import SourceQuality
from app.tuning import build_tuning
from app.validate_config import validate

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 1, 5, 7, 0, tzinfo=IST)
SEEDS = load_seeds(SERVICE_ROOT / "data" / "event_seeds.csv")
BY_ID = {s.seed_id: s for s in SEEDS}
RULES = CompanyRules.load(SERVICE_ROOT / "data" / "nifty_heavyweights.yaml")
QUALITY = SourceQuality.load(SERVICE_ROOT / "data" / "source_quality.yaml")


def _art(i, title="Some headline", source="Reuters"):
    return Article(f"a{i}", source, f"http://x/{i}", title, "", NOW, NOW, "india", "rss", source_id=source)


def _link(i, seed="M01", conf=0.6, sources=1, cluster=None, source="Reuters", **kw):
    seed_obj = BY_ID[seed]
    return RelevantArticle(_art(i, source=source), seed, seed_obj.family, seed_obj.geography, 1.0, (), match_confidence=conf,
                           cluster_id=cluster or f"c{i}", cluster_size=sources, cluster_sources=sources, **kw)


def _ok(**over):
    base = {"event_status": "occurred", "evidence": "", "relates": True, "relation_confidence": 0.9, "direction": 1, "intensity": 0.6,
            "confidence": 0.8, "surprise": "beat", "is_commentary": False, "already_priced": False, "rationale": "ok"}
    return {**base, **over}


class Fake:
    name, model = "fake", "fake-model"

    def __init__(self, **over):
        self.over, self.calls = over, 0

    def complete(self, system, user, max_tokens=3000):
        self.calls += 1
        n = user.count("### ITEM ")
        return LlmResponse(json.dumps({"results": [{"id": i + 1, **_ok(**self.over)} for i in range(n)]}), "fake-model", 1, 1, 0.0)


def _judge(tmp_path, client=None, quality=None, tuning=None):
    return LlmJudge(client or Fake(), LlmCache(tmp_path / "c.sqlite3"), SEEDS, tuning or build_tuning({}), source_quality=quality)


# --- 1. rank events by importance, not similarity alone --------------------------------------------------------------------
def test_wide_coverage_outranks_a_lone_outlet_with_slightly_higher_similarity():
    t = build_tuning({})
    assert event_importance(_link(1, conf=0.51, sources=15), "P1", t) > event_importance(_link(2, conf=0.62, sources=1), "P1", t)
    assert event_importance(_link(3, conf=0.6), "P2", t) < event_importance(_link(4, conf=0.6), "P1", t)


def test_the_cap_keeps_the_most_important_events_and_reports_the_cut():
    t = build_tuning({"similarity": {"max_events_to_judge": 2}})
    links = [_link(1, conf=0.62, sources=1), _link(2, conf=0.51, sources=17), _link(3, conf=0.55, sources=2), _link(4, conf=0.47, sources=1)]
    kept, cut = select_top_events(links, BY_ID, t)
    assert cut == 2 and {l.cluster_id for l in kept} == {"c2", "c3"}  # the 17-outlet story survives; the lone 0.62 does not
    assert select_top_events(links, BY_ID, build_tuning({})) == (links, 0)  # under the cap nothing is touched


# --- 2. one event = one full signal ---------------------------------------------------------------------------------------------
def _j(i, seed, group, cluster, direction=-1, intensity=0.6):
    return ImpactJudgement(f"a{i}", seed, "fam", direction, 0.9, intensity, "r", "m", shock_group=group, cluster_id=cluster)


def test_an_event_reaches_the_final_score_through_one_seed_only_by_default():
    assert Settings.model_fields["event_secondary_weight"].default == 0.0
    arts = {"a1": _art(1, "IEA cuts oil demand outlook"), "a2": _art(2, "IEA cuts oil demand outlook sharply")}
    same_event = [_j(1, "G02", "OIL_GEO", "evt"), _j(2, "G04", "INDIA_GEO", "evt", intensity=0.45)]
    result = aggregate(same_event, arts, event_secondary_weight=Settings.model_fields["event_secondary_weight"].default)
    assert sorted(r["event_weight"] for r in result.clusters) == [0.0, 1.0] and sorted(g.group for g in result.groups) == ["OIL_GEO"]


def test_the_same_event_under_two_seeds_in_different_groups_counts_once_in_full():
    arts = {"a1": _art(1, "Infosys DOJ probe on H-1B filings"), "a2": _art(1, "Infosys DOJ probe on H-1B filings")}
    arts["a2"] = _art(2, "Infosys DOJ probe on H-1B filings visas")
    same_event = [_j(1, "C05", "INDIA_CORP", "evt"), _j(2, "T05", "AI_TECH", "evt", intensity=0.45)]
    two_events = [_j(1, "C05", "INDIA_CORP", "evt1"), _j(2, "T05", "AI_TECH", "evt2", intensity=0.45)]
    once = aggregate(same_event, arts, event_secondary_weight=0.25)
    twice = aggregate(two_events, arts, event_secondary_weight=0.25)
    assert abs(once.score) < abs(twice.score)
    weights = sorted(r["event_weight"] for r in once.clusters)
    assert weights == [0.25, 1.0]  # the strongest seed in full, the other at the secondary weight
    assert aggregate(same_event, arts, event_secondary_weight=0.0).clusters  # strictly-once mode runs
    no_ids = [_j(1, "C05", "INDIA_CORP", ""), _j(2, "T05", "AI_TECH", "", intensity=0.45)]
    assert aggregate(no_ids, arts, event_secondary_weight=0.25).score == pytest.approx(twice.score)  # judgements without an event id are independent


# --- 3. source quality ---------------------------------------------------------------------------------------------------------
def test_source_quality_weights_come_from_the_yaml():
    assert QUALITY.weight("Facebook") == 0.3 and QUALITY.weight("LinkedIn") == 0.5 and QUALITY.weight("https://www.dars.gov.example") == 0.5
    assert QUALITY.weight("Reuters") == 1.0 and SourceQuality().weight("anything") == 1.0


def test_a_social_post_falls_under_the_confidence_gate_and_a_wire_story_does_not(tmp_path):
    judge = _judge(tmp_path, quality=QUALITY)
    wire, social = judge.judge([_link(1, source="Reuters"), _link(2, source="Facebook")])[0]
    assert social.confidence == pytest.approx(wire.confidence * 0.3) and social.details["source_weight"] == 0.3
    result = aggregate([wire, social], {"a1": _art(1), "a2": _art(2, "other words entirely")}, min_confidence=0.35)
    rows = {r["article_ids"][0]: r for r in result.clusters}
    assert rows["a2"]["gated_to_zero"] and not rows["a1"]["gated_to_zero"]


def test_the_trustworthy_source_represents_an_event_when_similarity_ties():
    fb, wire = _link(1, conf=0.60, cluster="evt", source="Facebook"), _link(2, conf=0.58, cluster="evt", source="Reuters")
    [kept] = consolidate_clusters([fb, wire], max_seeds=2, source_quality=QUALITY)
    assert kept.article.source == "Reuters"  # 0.58 x 1.0 beats 0.60 x 0.3
    [plain] = consolidate_clusters([fb, wire], max_seeds=2)
    assert plain.article.source == "Facebook"  # without quality weights similarity decides


# --- 4. freshness + provenance (decided vs expected) ----------------------------------------------------------------------------
def test_prompt_v2_asks_for_freshness_and_provenance_and_the_parser_reads_it():
    assert PROMPT_VERSION == "v5" and "event_status" in SYSTEM_PROMPT and "set to" in SYSTEM_PROMPT and "provenance" in SYSTEM_PROMPT and "unverified" in SYSTEM_PROMPT
    parsed, _ = parse_response(json.dumps({"results": [
        {"id": 1, "event_status": "occurred", "relates": True, "provenance": "Unverified"},
        {"id": 2, "event_status": "occurred", "relates": True, "provenance": "gibberish"},
        {"id": 3, "event_status": "occurred", "relates": True}]}), [1, 2, 3])
    assert [parsed[k].provenance for k in (1, 2, 3)] == ["unverified", "reported", "reported"]  # unknown/missing -> reported


def test_unverified_claims_and_expectations_never_score_by_default(tmp_path):
    [verified] = _judge(tmp_path / "a", Fake(provenance="official")).judge([_link(1)])[0]
    [unverified], report = _judge(tmp_path / "b", Fake(provenance="unverified")).judge([_link(1)])
    [expected] = _judge(tmp_path / "c", Fake(provenance="expectation")).judge([_link(1)])[0]
    assert unverified.direction == 0 and unverified.details["status"] == "unverified" and report.vetoed == 1
    assert expected.direction == 0 and expected.details["status"] == "expectation"  # a forecast is not an event
    assert verified.details["provenance"] == "official"
    tuned = build_tuning({"llm": {"provenance_factors": {"expectation": 0.3}}})
    [discounted] = _judge(tmp_path / "d", Fake(provenance="expectation"), tuning=tuned).judge([_link(1)])[0]
    assert discounted.direction == 1 and discounted.intensity == pytest.approx(verified.intensity * 0.3)  # the factor is tunable


def test_new_judgements_carry_the_event_id(tmp_path):
    [j], _ = _judge(tmp_path).judge([_link(1, cluster="evt-7")])
    assert j.cluster_id == "evt-7"


# --- 5. heavyweight corporate events ------------------------------------------------------------------------------------------------
def test_heavyweight_listing_seed_exists_needs_a_named_heavyweight_and_ignores_ipo_chatter():
    assert len(SEEDS) == 61 and "C07" in RULES.required_seeds and BY_ID["C07"].shock_group == "INDIA_CORP"
    art = Article("j", "Indian Express", "u", "How Jio's listing pushed Reliance past Tatas and Adanis", "", NOW, NOW, "india", "rss")
    got = match_articles([art], SEEDS, company_rules=RULES)
    assert any(m.seed_id == "C07" and "Reliance Industries" in m.heavyweights for m in got)
    small = Article("k", "ET", "u", "Obscure Foods listing: grey market premium today, allotment status", "", NOW, NOW, "india", "rss")
    assert not any(m.seed_id == "C07" for m in match_articles([small], SEEDS, company_rules=RULES))


# --- config plumbing -----------------------------------------------------------------------------------------------------------------
def test_source_quality_file_is_part_of_the_validated_and_bootstrapped_config(tmp_path):
    assert "source_quality_path" in CONFIG_FILES and validate(Settings())[0] == []
    for name in CONFIG_FILES.values():
        shutil.copy2(PACKAGED_CONFIG_DIR / name, tmp_path / name)
    (tmp_path / "source_quality.yaml").write_text("rules:\n  - {pattern: '(unclosed', weight: 0.5}\n  - {pattern: 'ok', weight: 1.5}\n", encoding="utf-8")
    errors = " | ".join(validate(Settings(config_dir=tmp_path))[0])
    assert "bad rule" in errors and "within 0..1" in errors
