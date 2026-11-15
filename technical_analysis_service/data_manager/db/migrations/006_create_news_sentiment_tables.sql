-- NIFTY news sentiment persistence.
-- These tables mirror the daily CSV outputs under output/intelligence while keeping
-- the production Render job independent of local filesystem persistence.

CREATE TABLE IF NOT EXISTS "NewsArticle" (
    article_id   varchar(64) PRIMARY KEY,
    source       varchar(200),
    url          text,
    title        text,
    summary      text,
    published_at timestamptz,
    fetched_at   timestamptz,
    region       varchar(50),
    provider     varchar(50),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_news_article_published_at
    ON "NewsArticle" (published_at);
CREATE INDEX IF NOT EXISTS ix_news_article_provider_region
    ON "NewsArticle" (provider, region);

CREATE TABLE IF NOT EXISTS "NewsArticleSentiment" (
    target_date               date NOT NULL,
    article_id                varchar(64) NOT NULL,
    window_start              timestamptz,
    window_end                timestamptz,
    sentiment_label           varchar(20),
    sentiment_score           double precision,
    sentiment_confidence      double precision,
    sentiment_model           varchar(100),
    sectors                   text,
    sector_confidences        text,
    sector_weight             double precision,
    weighted_sentiment        double precision,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_news_article_sentiment PRIMARY KEY (target_date, article_id),
    CONSTRAINT fk_news_article_sentiment_article
        FOREIGN KEY (article_id) REFERENCES "NewsArticle"(article_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_news_article_sentiment_target_date
    ON "NewsArticleSentiment" (target_date);
CREATE INDEX IF NOT EXISTS ix_news_article_sentiment_label
    ON "NewsArticleSentiment" (target_date, sentiment_label);

CREATE TABLE IF NOT EXISTS "NiftyMarketSentiment" (
    target_date                date PRIMARY KEY,
    window_start               timestamptz,
    window_end                 timestamptz,
    article_count              integer,
    usable_article_count       integer,
    composite_score            double precision,
    composite_label            varchar(20),
    mean_confidence            double precision,
    positive_count             integer,
    neutral_count              integer,
    negative_count             integer,
    weighted_signal_sum        double precision,
    normalization_denominator  double precision,
    source_mix                 text,
    generated_at               timestamptz,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_nifty_market_sentiment_label
    ON "NiftyMarketSentiment" (target_date, composite_label);
