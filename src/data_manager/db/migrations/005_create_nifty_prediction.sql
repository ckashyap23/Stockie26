-- Daily NIFTY final prediction (precision cascade output).
-- One row per (symbol, trade_date, model_version). Mirrors the production CSV at
-- output/backtest/NIFTY/production/NIFTY_prediction.csv: raw market data (prices,
-- volume, India VIX), the cascade's final_prediction and the
-- realised actual_trade_label. The technical feature columns are intentionally NOT
-- stored here (they live in "SignalFeatureDaily"); the prediction record keeps only
-- what is needed to inspect, grade and serve the daily call.
--
-- actual_trade_label is NULL until the next-day outcome lands (the row is "pending"
-- when first written for day n+1 and is filled in on a later run).

CREATE TABLE IF NOT EXISTS "NiftyPrediction" (
    symbol             varchar(50)  NOT NULL DEFAULT 'NIFTY',
    trade_date         date         NOT NULL,
    model_version      varchar(50)  NOT NULL DEFAULT 'cascade_v1',
    next_trade_date    date,
    open_915           double precision,
    high_day           double precision,
    low_day            double precision,
    close_1515         double precision,
    volume_day         double precision,
    vix_close          double precision,
    vix_chg_1d         double precision,
    vix_chg_pct        double precision,
    next_open          double precision,
    next_high          double precision,
    next_low           double precision,
    next_close         double precision,
    next_return_pct    double precision,
    final_prediction   varchar(20),
    direction          varchar(20),
    primary_strategy   varchar(120),
    strategy_precision double precision,
    signal_style       varchar(50),
    strength_score     double precision,
    strength_label     varchar(20),
    confidence_level   double precision,
    actual_trade_label varchar(20),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_nifty_prediction PRIMARY KEY (symbol, trade_date, model_version)
);

CREATE INDEX IF NOT EXISTS ix_nifty_prediction_date
    ON "NiftyPrediction" (trade_date);
