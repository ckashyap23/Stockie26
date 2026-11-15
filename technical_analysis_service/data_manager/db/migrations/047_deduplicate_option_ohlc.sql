WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY option_instrument_id, trade_date, candle_interval
            ORDER BY
                CASE
                    WHEN data_source = 'KITE_HISTORICAL_DAY_OHLC' THEN 0
                    ELSE 1
                END,
                loaded_at DESC,
                id DESC
        ) AS row_rank
    FROM "OptionOhlc"
)
DELETE FROM "OptionOhlc" target
USING ranked
WHERE target.id = ranked.id
  AND ranked.row_rank > 1;

ALTER TABLE "OptionOhlc"
    DROP CONSTRAINT IF EXISTS uq_option_ohlc_instrument_date_interval_source;

ALTER TABLE "OptionOhlc"
    ADD CONSTRAINT uq_option_ohlc_instrument_date_interval
    UNIQUE (option_instrument_id, trade_date, candle_interval);
