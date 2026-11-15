from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.calibration.model import Calibration, fit
from app.config import SERVICE_ROOT
from app.domain import Article, RelevantArticle
from app.pipeline.dedupe import EventCluster, cluster_articles, fair_cap
from app.pipeline.embedding import TfidfEmbedder
from app.pipeline.extract import extract_body, extract_full_text, fetcher_for
from app.pipeline.exclusion import first_exclusion
from app.pipeline.relevance import UNSEEDED, consolidate_clusters, match_articles
from app.pipeline.seeds import load_seeds
from app.storage.archive import ArticleArchive

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 1, 5, 7, 0, tzinfo=IST)
SEEDS = load_seeds(SERVICE_ROOT / "data" / "event_seeds.csv")


def _a(i, title, summary="", source="S", source_id="S1", prio="P2", published=T0, **kw):
    return Article(i, source, f"http://x/{i}", title, summary, published, published, "india", "rss",
                   source_id=source_id, source_priority=prio, **kw)


def _relevant(article, seed_id, **kw):
    seed = next(s for s in SEEDS if s.seed_id == seed_id)
    return RelevantArticle(article, seed_id, seed.family, seed.geography, kw.pop("relevance", 1.0), (), **kw)


# --- dedupe ------------------------------------------------------------------------------------------
def test_paraphrased_headlines_collapse_and_p1_source_is_representative():
    arts = [
        _a("a", "Trump announces 25% tariff on Indian exports", source_id="S9", prio="P2"),
        _a("b", "Trump announces 25% tariff on Indian exports, markets fall", source_id="S1", prio="P1"),
        _a("c", "RBI keeps repo rate unchanged at 5.25%"),
    ]
    clusters = cluster_articles(arts, TfidfEmbedder(), threshold=0.55)
    assert len(clusters) == 2
    tariff = next(c for c in clusters if len(c.members) == 2)
    assert tariff.representative.article_id == "b" and tariff.sources == 2


# --- relevance with confidence + catch-all ------------------------------------------------------------------
def test_unseeded_catch_all_needs_two_high_impact_words():
    hit = match_articles([_a("a", "Panic and meltdown as Atlantis exchange crashes")], SEEDS, unseeded_catch_all=True)
    miss = match_articles([_a("b", "Celebrity wedding photos")], SEEDS, unseeded_catch_all=True)
    assert [m.seed_id for m in hit] == [UNSEEDED.seed_id] and not miss


class ConceptEmbedder:
    """Deterministic stand-in for a dense model: one axis per concept, a text scores 1 on every concept it mentions."""

    name, kind, fallback_reason = "fake-dense", "dense", ""
    CONCEPTS = (("tax", "gst", "goods and services"), ("oil", "crude"), ("repo", "mpc", "policy rate"))

    def embed(self, texts):
        out = []
        for text in texts:
            low = text.lower()
            vec = [1.0 if any(w in low for w in words) else 0.0 for words in self.CONCEPTS]
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec] + [0.0])
        return out


class OrthogonalEmbedder(ConceptEmbedder):
    """Every headline is dissimilar to every seed: similarity is 0 everywhere."""

    def embed(self, texts):
        return [[0.0, 0.0, 0.0, 1.0] if i % 2 else [0.0, 0.0, 0.0, 1.0] for i, _ in enumerate(texts)]


def test_similarity_recovers_a_paraphrase_the_aliases_miss():
    art = _a("a", "Government lowers goods and services tax slabs on consumer goods")
    kw_only = match_articles([art], SEEDS)
    with_sim = match_articles([art], SEEDS, embedder=ConceptEmbedder())
    assert not any(m.seed_id == "F02" for m in kw_only)
    assert any(m.seed_id == "F02" for m in with_sim)


def test_keywords_only_lift_a_score_they_cannot_make_a_headline_relevant():
    art = _a("a", "RBI MPC holds repo rate; hints at CRR change")  # strong keyword hits
    assert match_articles([art], SEEDS)  # keyword-only path matches it
    assert match_articles([art], SEEDS, embedder=OrthogonalEmbedder()) == []  # zero similarity: boost alone (<= 0.2) < gate


def test_gate_decides_and_is_overridable():
    art = _a("a", "Oil prices climb on supply worries")
    assert any(m.seed_id.startswith(("E", "G")) for m in match_articles([art], SEEDS, embedder=ConceptEmbedder(), gate=0.3))
    assert match_articles([art], SEEDS, embedder=ConceptEmbedder(), gate=1.01) == []


def test_sparse_embedder_embeds_seed_texts_and_headlines_together():
    art = _a("a", "RBI MPC holds repo rate; hints at CRR change")
    got = match_articles([art], SEEDS, embedder=TfidfEmbedder())
    assert any(m.seed_id == "M01" for m in got)


def test_seed_text_strips_the_generic_nifty_word():
    from app.pipeline.relevance import seed_parts

    c01 = next(s for s in SEEDS if s.seed_id == "C01")
    assert all("nifty" not in part.lower() for part in seed_parts(c01))


# --- exclusion cues are filters, never entry requirements ------------------------------------------------------------------------
# --- archive: timestamped versions ---------------------------------------------------------------------------------
def test_archive_keeps_first_published_and_flags_revisions(tmp_path):
    archive = ArticleArchive(tmp_path / "a.sqlite3")
    first = _a("a", "RBI cuts repo rate", published=T0)
    archive.record([first])
    revised = _a("a", "RBI cuts repo rate by 25 bps", published=T0 + timedelta(hours=3))  # after the cutoff
    [canon] = archive.record([revised])
    assert canon.published_at == T0 and canon.updated_at is not None
    assert archive.stats()["revised"] == 1


def test_replay_excludes_items_not_yet_known_at_cutoff(tmp_path):
    archive = ArticleArchive(tmp_path / "a.sqlite3")
    cutoff = datetime(2026, 1, 5, 9, 0, tzinfo=IST)
    early = _a("a", "early", published=T0)
    late_seen = Article("b", "S", "http://x/b", "published early, ingested late", "", T0, cutoff + timedelta(hours=2),
                        "india", "rss")
    on_time = Article("d", "S", "http://x/d", "run started 08:45", "", T0, cutoff + timedelta(minutes=15), "india", "rss")
    after = _a("c", "after cutoff", published=cutoff + timedelta(minutes=30))
    archive.record([early, late_seen, on_time, after])
    got = {a.article_id for a in archive.as_of(cutoff - timedelta(hours=17), cutoff)}
    assert got == {"a", "d"}  # d was seen during the daily run, within grace


# --- full-text extraction ------------------------------------------------------------------------------------------------
PARA = "The central bank held rates steady and said inflation remains within its target band for now. "
HTML_P = f"<html><body><nav><p>{PARA}</p></nav><article><p>{PARA * 2}</p><p>{PARA * 3}</p></article></body></html>"
HTML_LD = '<script type="application/ld+json">{"@type":"NewsArticle","articleBody":"' + PARA * 5 + '"}</script>'


def test_extract_body_prefers_jsonld_then_article_paragraphs():
    assert extract_body(HTML_LD).startswith("The central bank")
    body = extract_body(HTML_P)
    assert body.count("central bank") == 5  # the <nav> paragraph is skipped


def test_site_fetcher_selection_by_domain():
    assert type(fetcher_for("https://rbi.org.in/x")).__name__ == "PublicRecordFetcher"
    assert type(fetcher_for("https://economictimes.indiatimes.com/a")).__name__ == "TimesGroupFetcher"
    assert type(fetcher_for("https://unknown.example/a")).__name__ == "SiteFetcher"


def test_headline_only_sources_are_never_fetched(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network used for a headline-only source")

    monkeypatch.setattr("app.pipeline.extract.requests.get", boom)
    item = _relevant(_a("a", "Bloomberg headline", body_mode="headline"), "M04")
    [out], status = extract_full_text([item], cache_dir=tmp_path, domain_delay=0)
    assert out.article.content_source == "headline_only" and "1 headline-only" in status


def test_fulltext_fetch_caches_and_falls_back_on_short_pages(tmp_path, monkeypatch):
    calls = []

    class Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        calls.append(url)
        return Resp(HTML_P if url.endswith("/ok") else "<html><body><p>tiny</p></body></html>")

    monkeypatch.setattr("app.pipeline.extract.requests.get", fake_get)
    ok = _relevant(Article("a", "S", "http://x/ok", "t", "summary", T0, T0, "india", "rss"), "M04")
    short = _relevant(Article("b", "S", "http://x/short", "t", "summary", T0, T0, "india", "rss"), "M04")
    out, _ = extract_full_text([ok, short], cache_dir=tmp_path, domain_delay=0, min_chars=300)
    assert [o.article.content_source for o in out] == ["full_text", "summary"]
    extract_full_text([ok], cache_dir=tmp_path, domain_delay=0, min_chars=300)
    assert calls.count("http://x/ok") == 1  # second run served from cache


# --- calibration -----------------------------------------------------------------------------------------------------------
def test_calibration_needs_enough_sessions_and_maps_score_to_bounded_magnitude(tmp_path):
    with pytest.raises(ValueError):
        fit([("2026-01-01", 0.1, 0.001)], min_rows=60)
    rows = [(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", s, 0.01 * s + (0.002 if i % 2 else -0.002))
            for i, s in enumerate([(-1) ** i * (0.1 + (i % 7) / 10) for i in range(90)])]
    cal = fit(rows)
    assert cal.slope > 0 and cal.validation["sign_hit_rate"] > 0.5
    assert 0 <= cal.magnitude(cal.predicted_return(0.9)) <= 1 and cal.magnitude(1.0) <= 1
    cal.save(tmp_path / "c.json")
    assert Calibration.load(tmp_path / "c.json").slope == cal.slope


# --- precision guards ----------------------------------------------------------------------------------------------------------
def test_summary_only_single_alias_does_not_match_but_title_hit_does():
    seeds = SEEDS
    stray = _a("a", "How much can you spend from your retirement corpus", summary="Not a withdrawal of troops, just money.")
    assert not any(m.seed_id == "G03" for m in match_articles([stray], seeds))
    real = _a("b", "Ceasefire talks resume in Gulf", summary="")
    assert any(m.seed_id == "G03" for m in match_articles([real], seeds))


def test_context_terms_required_for_foreign_enforcement_seed():
    domestic_us = _a("a", "DOJ opens probe into alleged threats to Republicans")
    indian = _a("b", "DOJ indictment names Adani executives in fraud probe")
    assert not any(m.seed_id == "C05" for m in match_articles([domestic_us], SEEDS))
    assert any(m.seed_id == "C05" for m in match_articles([indian], SEEDS))


def test_gulf_gas_attack_does_not_match_india_pakistan_seed():
    assert not any(m.seed_id == "G01" for m in match_articles([_a("a", "Iraq gas field suspends output after drone attack")], SEEDS))


# --- stock-pick: commentary filter + heavyweight gate -------------------------------------------------------------------
from app.pipeline.commentary import CommentaryFilter  # noqa: E402
from app.pipeline.company_gate import CompanyRules  # noqa: E402

COMMENTARY = CommentaryFilter.load(SERVICE_ROOT / "data" / "commentary_filters.yaml")
RULES = CompanyRules.load(SERVICE_ROOT / "data" / "nifty_heavyweights.yaml")


@pytest.mark.parametrize("title", [
    "CLSA sees 72% upside for this 'high-conviction' PSU stock after Q2 results",
    "Motilal Oswal initiates buy on Tata Motors, target price Rs 900",
    "5 stocks to buy today: brokerages recommend these picks",
    "Jefferies upgrades Infosys to buy; raises price target",
    "Top picks for Samvat 2083 from Kotak Securities",
])
def test_broker_calls_and_stock_picks_are_dropped(title):
    assert COMMENTARY.classify(_a("x", title)) is not None


@pytest.mark.parametrize("title", [
    "Goldman Sachs raises India GDP forecast to 6.8%",
    "Brokerage cuts India growth forecast on tariff drag",
    "RBI maintains neutral stance, keeps repo rate unchanged",
    "HDFC Bank Q2 profit jumps 12%, beats estimates",
    "Fed holds rates, signals one more cut",
])
def test_macro_and_real_company_news_are_not_dropped(title):
    assert COMMENTARY.classify(_a("x", title)) is None


def test_company_seed_needs_a_named_heavyweight_and_scales_by_weight():
    stray = _a("a", "Little-known PSU firm reports Q2 results, profit rises")
    big = _a("b", "HDFC Bank Q2 results: profit rises, asset quality stable")
    small = _a("c", "Trent Q2 results: profit rises")
    got = match_articles([stray, big, small], SEEDS, company_rules=RULES)
    by_article = {m.article.article_id: m for m in got if m.seed_id == "C01"}
    assert "a" not in by_article  # no heavyweight named -> dropped
    assert by_article["b"].weight_scale == 1.0 and by_article["b"].heavyweights == ("HDFC Bank",)
    assert 0 < by_article["c"].weight_scale < 0.2  # 0.7% of the 5% full-weight reference


def test_sector_wide_c03_does_not_need_a_named_company():
    got = match_articles([_a("a", "Auto makers report strong monthly sales in October as festive demand lifts volumes")], SEEDS, company_rules=RULES)
    assert any(m.seed_id == "C03" for m in got)


def test_heavyweight_named_only_in_the_summary_does_not_qualify():
    roundup = _a("a", "Q2 Results Highlights: Siemens revenue expands 17%; Blue Dart profit jumps",
                 summary="Also in focus: Bharti Airtel, Titan and ONGC reported results this week.")
    assert not any(m.seed_id == "C01" for m in match_articles([roundup], SEEDS, company_rules=RULES))


# --- config consolidation ---------------------------------------------------------------------------------------------
import shutil  # noqa: E402

from app.config import CONFIG_FILES, PACKAGED_CONFIG_DIR, Settings, bootstrap_config  # noqa: E402
from app.pipeline.seeds import load_sources  # noqa: E402
from app.tuning import build_tuning  # noqa: E402
from app.validate_config import validate  # noqa: E402


def test_shipped_config_validates_with_no_errors():
    errors, _ = validate(Settings())
    assert errors == []


def test_validator_catches_bad_regex_unknown_seed_and_conflicting_cue(tmp_path):
    for name in CONFIG_FILES.values():
        shutil.copy2(PACKAGED_CONFIG_DIR / name, tmp_path / name)
    (tmp_path / "commentary_filters.yaml").write_text("categories:\n  broker_call:\n    - '(unclosed'\n", encoding="utf-8")
    hw = (tmp_path / "nifty_heavyweights.yaml").read_text(encoding="utf-8").replace("[C01, C02, C04, C07]", "[C01, ZZ9]")
    (tmp_path / "nifty_heavyweights.yaml").write_text(hw, encoding="utf-8")
    seeds = (tmp_path / "event_seeds.csv").read_text(encoding="utf-8-sig")
    seeds = seeds.replace(",INDIA_RATES,,", ",INDIA_RATES,repo rate,", 1)  # M01: an exclude cue that is also one of its own aliases
    (tmp_path / "event_seeds.csv").write_text(seeds, encoding="utf-8-sig")
    errors, _ = validate(Settings(config_dir=tmp_path))
    text = " | ".join(errors)
    assert "bad regex" in text and "ZZ9" in text and "also aliases" in text


def test_bootstrap_copies_missing_defaults_but_never_overwrites(tmp_path):
    cfg = tmp_path / "config"
    settings = Settings(config_dir=cfg)
    assert sorted(bootstrap_config(settings)) == sorted(CONFIG_FILES.values())
    (cfg / "tuning.yaml").write_text("llm: {batch_size: 3}\n", encoding="utf-8")
    assert bootstrap_config(settings) == []  # nothing missing; the edit survives
    assert "batch_size: 3" in (cfg / "tuning.yaml").read_text(encoding="utf-8")
    assert settings.seeds_path == cfg / "event_seeds.csv"


def test_seed_file_edits_are_picked_up_without_a_restart(tmp_path):
    path = tmp_path / "seeds.csv"
    shutil.copy2(PACKAGED_CONFIG_DIR / "event_seeds.csv", path)
    first = load_seeds(path)
    text = path.read_text(encoding="utf-8-sig").replace("MPC;repo rate;policy rate", "MPC;repo rate;policy rate;standing deposit facility", 1)
    path.write_text(text, encoding="utf-8-sig")
    import os
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000_000))
    assert "standing deposit facility" in load_seeds(path)[0].aliases and "standing deposit facility" not in first[0].aliases


def test_sources_come_from_feeds_yaml():
    sources = load_sources(PACKAGED_CONFIG_DIR / "feeds.yaml")
    assert len(sources) == 18 and all(s.feeds for s in sources)
    assert next(s for s in sources if s.source_id == "S11").body == "headline"


# --- benchmark regression guard (needs the real model; skipped when it cannot load) ----------------------------------------------
def test_production_similarity_recipe_meets_the_benchmark_floor():
    pytest.importorskip("sentence_transformers")
    from app.eval import relevance_benchmark as rb

    try:
        results = rb.run(["st:sentence-transformers/all-mpnet-base-v2"])
    except Exception as exc:  # noqa: BLE001 - e.g. offline and the model is not cached
        pytest.skip(f"model unavailable: {exc}")
    prod = next(r for r in results if r["doc"].startswith("PRODUCTION") and r["boost"] == 0.2)
    keyword = next(r for r in results if r["embedder"].startswith("keyword"))
    assert prod["top1"] >= 0.70 and prod["top3"] >= 0.85 and prod["auroc"] >= 0.85
    assert prod["top1"] > keyword["top1"] + 0.2  # similarity must beat the keyword gate by a wide margin



# --- exclusion cues, new seeds, fair cap, cluster consolidation -----------------------------------------------------------------------
def test_exclude_cue_filters_a_candidate_out_and_is_logged():
    # enough alias hits (LoC, drone, missile) to pass the keyword gate, so the exclusion filter is what removes it
    art = _a("a", "History of the LoC: how India and Pakistan used drone and missile strikes in the 1965 Rann of Kutch")
    trace = []
    got = match_articles([art], SEEDS, trace=trace)
    assert not any(m.seed_id == "G01" for m in got)
    assert any(t["seed_id"] == "G01" and t["reject_reason"].startswith("excluded_by_cue:") for t in trace)
    assert first_exclusion("India vs Pakistan cricket match preview", ("cricket",)) == "cricket"
    assert first_exclusion("Pakistan troops fire along the LoC", ("cricket", "history")) is None


def test_an_exclude_cue_never_admits_anything():
    # a headline made only of exclusion words matches no seed: cues remove candidates, they cannot create them
    assert match_articles([_a("a", "movie film documentary anniversary history")], SEEDS) == []


def test_not_a_fresh_event_headlines_are_removed_before_dedupe():
    filt = CommentaryFilter.load(SERVICE_ROOT / "data" / "commentary_filters.yaml")
    for title in ("What to know about Canada's escalating trade war with the U.S.", "Explained: why the rupee is falling",
                  "War history: How India fought Pakistan in 1965", "Opinion: the budget will not fix growth"):
        hit = filt.classify(_a("x", title))
        assert hit is not None and hit.category == "not_a_fresh_event", title
    for title in ("The ECB Just Raised Interest Rates. Here's What to Know.", "Brent crude oil tops $101 to close at highest level since May",
                  "Indian rupee slumps 38 paise to close at 95.46",
                  "FIIs sell Rs 3,000 crore of Indian shares", "Fed holds rates, signals one more cut"):
        assert filt.classify(_a("x", title)) is None, title


def test_new_seeds_exist_with_guidance_and_shock_groups():
    by_id = {s.seed_id: s for s in SEEDS}
    assert len(SEEDS) == 61
    for sid, group in (("E05", "OIL_GEO"), ("R01", "FX_FLOWS"), ("R02", "FX_FLOWS"), ("R03", "GLOBAL_RISK"), ("R04", "GLOBAL_RATES")):
        assert by_id[sid].shock_group == group and by_id[sid].llm_guidance and by_id[sid].aliases, sid
    assert "pakistani rupee" in by_id["R02"].exclude_cues  # other countries' rupees are not ours


def test_the_headlines_that_were_lost_now_match_the_new_seeds():
    # keyword path: the aliases of the new seeds are enough to admit the headlines we traced for 9 and 11 Sep 2026
    cases = {
        "Brent crude oil tops $101 to close at highest level since May as fighting escalates": "E05",
        "Indian rupee slumps 38 paise to close at 95.46 against US dollar": "R02",
        "FII DII Data Today: FIIs Sell, DIIs Buy Indian Equities": "R01",
    }
    for title, seed in cases.items():
        assert any(m.seed_id == seed for m in match_articles([_a("x", title)], SEEDS)), title


def test_fair_cap_spreads_the_cut_over_days_and_sources_and_keeps_order():
    day1, day2 = T0, T0 + timedelta(days=1)
    arts = ([_a(f"f{i}", f"friday {i}", source=f"S{i % 3}", source_id=f"S{i % 3}", published=day1) for i in range(60)]
            + [_a(f"s{i}", f"saturday {i}", source=f"S{i % 3}", source_id=f"S{i % 3}", published=day2) for i in range(6)])
    kept, cut = fair_cap(arts, 30)
    assert cut == 36 and len(kept) == 30
    assert sum(a.article_id.startswith("s") for a in kept) == 6  # the small Saturday is NOT crowded out by the big Friday
    assert [a.article_id for a in kept] == [a.article_id for a in arts if a in kept]  # original order preserved
    assert fair_cap(arts, 500) == (arts, 0)  # under the cap nothing changes


def test_dense_clustering_matches_expected_groups():
    class Dense(ConceptEmbedder):
        pass

    arts = [_a("a", "oil price jumps"), _a("b", "crude surges again"), _a("c", "repo rate decision by MPC")]
    sizes = sorted(len(c.members) for c in cluster_articles(arts, Dense(), threshold=0.9))
    assert sizes == [1, 2]  # the two oil headlines merge, the rates headline stays alone


def test_every_member_of_a_cluster_is_matched_and_the_cluster_keeps_its_best_link_per_seed():
    oil_rep = _a("rep", "Nifty Sensex Today: Nifty climbs 46 points as banking stocks snap losing streak", prio="P1")
    oil_member = _a("mem", "NIFTY50 falls below 23,250 as crude tops $109 per barrel")
    cluster = EventCluster("c-rep", oil_rep, (oil_rep, oil_member))  # a (wrong) merge of two different headlines
    by_member = {m.article_id: cluster for m in cluster.members}
    links = match_articles(list(cluster.members), SEEDS, embedder=ConceptEmbedder(), gate=0.3, clusters=by_member)
    kept = consolidate_clusters(links, max_seeds=2)
    assert len(cluster.members) == 2
    assert kept and all(l.cluster_id == cluster.cluster_id for l in kept)
    assert len({l.seed_id for l in kept}) == len(kept) <= 2  # one link per seed, at most two seeds
    assert any(l.article.article_id == "mem" for l in kept)  # the crude headline is what got matched, not the market-wrap rep
