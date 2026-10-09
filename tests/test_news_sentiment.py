from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.news_sentiment.config import NIFTY50_SECTOR_DEFINITIONS, article_sentiment_store_path, article_store_path, composite_signal_store_path, target_date_folder
from src.news_sentiment.article_store import append_enriched_articles
from src.news_sentiment.pipeline import run_news_sentiment_for_target, run_news_sentiment_pipeline, sentiment_window
from src.news_sentiment.schemas import EnrichedArticle, NewsArticle, SectorTag, SentimentResult
from src.news_sentiment.sector_classifier import KeywordSectorClassifier, LlmSectorClassifier, _normalize_llm_tags, _sector_prompt_options
from src.news_sentiment.sentiment import (
    FinBertSentimentScorer,
    HuggingFaceFinBertSentimentScorer,
    _build_hf_fallback_scorer,
    build_sentiment_scorer,
)
from src.news_sentiment.sector_weights import build_sector_weights_from_component_csv, load_nifty50_sector_weights, map_nse_industry_to_sector
from src.news_sentiment.weighting import build_composite_signal
from src.news_sentiment.weighting import sector_weight

IST = ZoneInfo("Asia/Kolkata")


def test_target_date_maps_to_overnight_premarket_window() -> None:
    window_start, window_end = sentiment_window(date(2026, 6, 25))

    assert window_start == datetime(2026, 6, 24, 15, 30, tzinfo=IST)
    assert window_end == datetime(2026, 6, 25, 9, 0, tzinfo=IST)


def test_target_date_partition_paths_use_day_month_year_folder() -> None:
    assert target_date_folder(date(2026, 6, 26)) == "26-06-2026"
    assert article_store_path("2026-06-26").as_posix().endswith("news_articles/26-06-2026/news_articles.csv")
    assert article_sentiment_store_path("2026-06-26").as_posix().endswith("article_sentiment/26-06-2026/NIFTY_article_sentiment.csv")
    assert composite_signal_store_path("2026-06-26").as_posix().endswith("market_sentiment/26-06-2026/NIFTY_market_sentiment.csv")


def test_article_model_text_uses_headline_and_summary_only() -> None:
    article = NewsArticle(
        article_id="a1",
        source="Test Source",
        url="https://example.test/article",
        title="RBI raises repo rate by 25bps",
        summary="Inflation concerns remain elevated.",
        published_at=datetime(2026, 6, 25, 8, 0, tzinfo=IST),
        fetched_at=datetime(2026, 6, 25, 8, 1, tzinfo=IST),
        region="india",
        provider="rss",
    )

    assert article.text_for_model() == "RBI raises repo rate by 25bps\n\nInflation concerns remain elevated."


def test_fallback_sentiment_batch_scores_without_transformers() -> None:
    scorer = FinBertSentimentScorer(use_transformers=False)

    results = scorer.score_many([
        "Stocks rally as inflows boost market optimism",
        "Inflation risk and selloff pressure markets",
        "Market opens flat before policy decision",
        "",
    ])

    assert [item.label for item in results] == ["positive", "negative", "neutral", "neutral"]
    assert [item.score for item in results] == [1.0, -1.0, 0.0, 0.0]
    assert results[0].model_name == "lexical_fallback"
    assert results[-1].model_name == "empty_text"


def test_huggingface_finbert_response_maps_to_sentiment(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")
    scorer = HuggingFaceFinBertSentimentScorer()

    result = scorer._from_hf_response([[
        {"label": "neutral", "score": 0.05},
        {"label": "positive", "score": 0.88},
        {"label": "negative", "score": 0.07},
    ]])

    assert result.label == "positive"
    assert result.score == 1.0
    assert result.confidence == 0.88
    assert result.model_name.startswith("hf_serverless:")


def test_sentiment_factory_selects_huggingface_scorer(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_SENTIMENT_SCORER", "hf_finbert")
    monkeypatch.setenv("HF_TOKEN", "test-token")

    scorer = build_sentiment_scorer(use_transformers=False)

    assert isinstance(scorer, HuggingFaceFinBertSentimentScorer)


def test_hf_fallback_can_select_local_finbert(monkeypatch) -> None:
    monkeypatch.setenv("HF_FINBERT_FALLBACK", "local_finbert")
    monkeypatch.setenv("FINBERT_LOCAL_MODEL_PATH", "models/ProsusAI/finbert")
    monkeypatch.setattr(FinBertSentimentScorer, "__post_init__", lambda self: setattr(self, "_pipeline", None))

    scorer = _build_hf_fallback_scorer()

    assert isinstance(scorer, FinBertSentimentScorer)
    assert scorer.use_transformers is True
    assert scorer.model_name == "models/ProsusAI/finbert"


def test_llm_sector_tags_are_validated_and_normalized() -> None:
    tags = _normalize_llm_tags([
        {"sector": "financial_services", "confidence": 0.70},
        {"sector": "broad_market", "confidence": 0.30},
        {"sector": "unknown", "confidence": 1.0},
    ])

    assert [tag.sector for tag in tags] == ["financial_services", "broad_market"]
    assert round(sum(tag.confidence for tag in tags), 6) == 1.0


def test_llm_sector_classifier_uses_azure_openai_env(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example-resource.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "nifty-sector-gpt")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    classifier = LlmSectorClassifier()

    assert classifier.resolved_provider == "azure"
    assert classifier._chat_url("azure") == (
        "https://example-resource.openai.azure.com/openai/deployments/"
        "nifty-sector-gpt/chat/completions"
    )
    assert classifier._params("azure") == {"api-version": "2024-12-01-preview"}


def test_llm_prompt_options_are_driven_by_sector_weight_config() -> None:
    prompt_options = _sector_prompt_options()
    by_sector = {item["sector"]: item["label"] for item in prompt_options}

    assert by_sector["financial_services"] == "Banking & Finance"
    assert by_sector["oil_gas"] == "Energy & Oil"
    assert by_sector["realty"] == "Realty"
    assert set(by_sector) >= {definition.key for definition in NIFTY50_SECTOR_DEFINITIONS}
    assert by_sector["broad_market"].startswith("Broad market")


def test_sector_classifier_keyword_fallback() -> None:
    classifier = KeywordSectorClassifier()

    tags = classifier.classify("RBI rate decision lifts banking stocks and bond yields")

    assert tags[0].sector == "financial_services"


def test_composite_signal_uses_weighted_sentiment_over_total_weight_hit() -> None:
    article = NewsArticle(
        article_id="a1",
        source="Test Source",
        url="https://example.test/article",
        title="Banks rally",
        summary="",
        published_at=datetime(2026, 6, 25, 8, 0, tzinfo=IST),
        fetched_at=datetime(2026, 6, 25, 8, 1, tzinfo=IST),
        region="india",
        provider="rss",
    )
    enriched = [
        EnrichedArticle(
            article=article,
            sentiment=SentimentResult("positive", 1.0, 0.80, "test"),
            sectors=[SectorTag("financial_services", 1.0, [])],
            sector_weight=0.33,
            weighted_sentiment=0.264,
        ),
        EnrichedArticle(
            article=article,
            sentiment=SentimentResult("negative", -1.0, 0.50, "test"),
            sectors=[SectorTag("oil_gas", 1.0, [])],
            sector_weight=0.12,
            weighted_sentiment=-0.060,
        ),
    ]

    signal = build_composite_signal(
        "2026-06-25",
        datetime(2026, 6, 24, 15, 30, tzinfo=IST),
        datetime(2026, 6, 25, 9, 0, tzinfo=IST),
        enriched,
        datetime(2026, 6, 25, 9, 1, tzinfo=IST),
    )

    assert signal.weighted_signal_sum == 0.204
    assert signal.normalization_denominator == 0.324
    assert signal.composite_score == round(0.204 / 0.324, 4)


def test_nse_industry_mapping_covers_key_nifty_sectors() -> None:
    assert map_nse_industry_to_sector("Financial Services") == "financial_services"
    assert map_nse_industry_to_sector("Information Technology") == "information_technology"
    assert map_nse_industry_to_sector("Oil Gas & Consumable Fuels") == "oil_gas"
    assert map_nse_industry_to_sector("Pharmaceuticals") == "healthcare"
    assert map_nse_industry_to_sector("Capital Goods") == "construction"
    assert map_nse_industry_to_sector("Unknown Experimental Industry") == "broad_market"


def test_sector_weight_prefers_cached_weight_mapping(tmp_path) -> None:
    cache = tmp_path / "weights.csv"
    cache.write_text(
        "sector_key,label,weight,weight_pct,constituent_count,source,fetched_at\n"
        "financial_services,Banking & Finance,0.40,40.0,10,test,2026-06-25T09:00:00+05:30\n",
        encoding="utf-8",
    )

    weights = load_nifty50_sector_weights(cache)

    assert weights["financial_services"] == 0.40
    assert weights["broad_market"] == 1.0
    assert sector_weight([SectorTag("financial_services", 1.0, [])], sector_weights=weights) == 0.40


def test_component_csv_builds_sector_weight_cache(tmp_path) -> None:
    component_csv = tmp_path / "components.csv"
    output_csv = tmp_path / "sector_weights.csv"
    component_csv.write_text(
        "symbol,market_cap,predicted_sector\n"
        "AAA,2T,financial_services\n"
        "BBB,1T,oil_gas\n"
        "CCC,500B,oil_gas\n",
        encoding="utf-8",
    )

    weights = build_sector_weights_from_component_csv(component_csv, output_path=output_csv)
    cached = load_nifty50_sector_weights(output_csv)

    assert output_csv.exists()
    assert weights.loc[weights["sector_key"] == "financial_services", "constituent_count"].iloc[0] == 1
    assert round(cached["financial_services"], 6) == round(2 / 3.5, 6)
    assert round(cached["oil_gas"], 6) == round(1.5 / 3.5, 6)


def test_append_enriched_articles_replaces_existing_target_date(tmp_path) -> None:
    path = tmp_path / "article_sentiment.csv"
    window_start = datetime(2026, 6, 24, 15, 30, tzinfo=IST)
    window_end = datetime(2026, 6, 25, 9, 0, tzinfo=IST)

    old_article = NewsArticle(
        article_id="old",
        source="Old Source",
        url="https://example.test/old",
        title="Old headline",
        summary="",
        published_at=datetime(2026, 6, 24, 16, 0, tzinfo=IST),
        fetched_at=datetime(2026, 6, 24, 16, 1, tzinfo=IST),
        region="india",
        provider="rss",
    )
    new_article = NewsArticle(
        article_id="new",
        source="New Source",
        url="https://example.test/new",
        title="New headline",
        summary="",
        published_at=datetime(2026, 6, 24, 17, 0, tzinfo=IST),
        fetched_at=datetime(2026, 6, 24, 17, 1, tzinfo=IST),
        region="india",
        provider="rss",
    )

    append_enriched_articles(
        [EnrichedArticle(old_article, SentimentResult("neutral", 0.0, 0.4, "old"), [SectorTag("broad_market", 1.0, [])], 1.0, 0.0)],
        "2026-06-25",
        window_start,
        window_end,
        path=path,
    )
    updated = append_enriched_articles(
        [EnrichedArticle(new_article, SentimentResult("positive", 1.0, 0.9, "new"), [SectorTag("broad_market", 1.0, [])], 1.0, 0.9)],
        "2026-06-25",
        window_start,
        window_end,
        path=path,
    )

    assert updated["article_id"].tolist() == ["new"]


def test_empty_article_outputs_are_not_written(tmp_path) -> None:
    from src.news_sentiment.article_store import append_articles

    article_path = tmp_path / "news_articles.csv"
    sentiment_path = tmp_path / "NIFTY_article_sentiment.csv"
    window_start = datetime(2026, 1, 1, 15, 30, tzinfo=IST)
    window_end = datetime(2026, 1, 2, 9, 0, tzinfo=IST)

    append_articles([], path=article_path)
    append_enriched_articles([], "2026-01-02", window_start, window_end, path=sentiment_path)

    assert not article_path.exists()
    assert not sentiment_path.exists()


def test_pipeline_skips_outputs_when_no_articles(monkeypatch) -> None:
    monkeypatch.setattr("src.news_sentiment.pipeline.fetch_all_articles", lambda *args, **kwargs: [])

    result = run_news_sentiment_pipeline(
        target_date=date(2026, 1, 2),
        include_newsapi=False,
        use_transformers=False,
        sector_classifier_mode="keyword",
    )

    assert result["article_count"] == 0
    assert result["composite_label"] == "skipped_no_articles"


def test_pipeline_persists_to_db_when_local_csv_outputs_fail(monkeypatch) -> None:
    article = NewsArticle(
        article_id="a1",
        source="Test Source",
        url="https://example.test/article",
        title="Banks rally",
        summary="NIFTY gains before the open.",
        published_at=datetime(2026, 6, 25, 8, 0, tzinfo=IST),
        fetched_at=datetime(2026, 6, 25, 8, 1, tzinfo=IST),
        region="india",
        provider="rss",
    )
    captured = {}

    class FakeScorer:
        def score_many(self, texts):
            return [SentimentResult("positive", 1.0, 0.8, "test")]

    class FakeSectorClassifier:
        def classify_many(self, texts):
            return [[SectorTag("broad_market", 1.0, [])]]

    def fail_write(*args, **kwargs):
        raise OSError("read-only filesystem")

    def fake_persist(articles, enriched, target_str, window_start, window_end, signal):
        captured["articles"] = articles
        captured["enriched"] = enriched
        captured["target_str"] = target_str
        captured["signal"] = signal
        return {"news_articles": 1, "article_sentiments": 1, "market_sentiments": 1}

    monkeypatch.setattr("src.news_sentiment.pipeline.fetch_all_articles", lambda *args, **kwargs: [article])
    monkeypatch.setattr("src.news_sentiment.pipeline.append_articles", fail_write)
    monkeypatch.setattr("src.news_sentiment.pipeline.append_enriched_articles", fail_write)
    monkeypatch.setattr("src.news_sentiment.pipeline.append_composite_signal", fail_write)
    monkeypatch.setattr("src.news_sentiment.pipeline.persist_news_sentiment_run", fake_persist)

    signal = run_news_sentiment_for_target(
        date(2026, 6, 25),
        scorer=FakeScorer(),
        sector_classifier=FakeSectorClassifier(),
        include_newsapi=False,
    )

    assert signal is not None
    assert signal.target_date == "2026-06-25"
    assert captured["articles"] == [article]
    assert captured["target_str"] == "2026-06-25"
    assert captured["enriched"][0].weighted_sentiment == 0.8
    assert captured["signal"].composite_label == "positive"
