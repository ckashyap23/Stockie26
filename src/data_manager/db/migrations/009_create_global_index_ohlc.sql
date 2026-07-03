-- Global index OHLC persistence for external macro/risk signals.
-- The daily loader writes here first; local CSV output is optional.

CREATE TABLE IF NOT EXISTS "GlobalIndexOhlc" (
    index_code    varchar(50) NOT NULL,
    index_name    varchar(120) NOT NULL,
    yahoo_symbol  varchar(50) NOT NULL,
    region        varchar(50),
    currency      varchar(10),
    trade_date    date NOT NULL,
    open_price    double precision,
    high_price    double precision,
    low_price     double precision,
    close_price   double precision,
    adj_close     double precision,
    volume        bigint,
    source        varchar(50) NOT NULL DEFAULT 'yfinance',
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_global_index_ohlc PRIMARY KEY (index_code, trade_date, source)
);

CREATE INDEX IF NOT EXISTS ix_global_index_ohlc_date
    ON "GlobalIndexOhlc" (trade_date);
CREATE INDEX IF NOT EXISTS ix_global_index_ohlc_symbol_date
    ON "GlobalIndexOhlc" (index_code, trade_date);