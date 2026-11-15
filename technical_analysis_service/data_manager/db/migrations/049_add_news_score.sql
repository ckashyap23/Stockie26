-- Daily news score, written by news_signal_service (python -m app.publish / the daily run).
-- Stored on the signal row whose next_trade_date is the trade date the score applies to
-- (signal_date + 1 trading day). Informational: no prediction logic reads these columns, and the
-- prediction upsert does not list them, so re-running predictions never overwrites them.
ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS news_score double precision,
    ADD COLUMN IF NOT EXISTS news_direction varchar(20),
    ADD COLUMN IF NOT EXISTS news_confidence double precision,
    ADD COLUMN IF NOT EXISTS news_run_id varchar(80),
    ADD COLUMN IF NOT EXISTS news_scored_at timestamptz;

COMMENT ON COLUMN "NiftyPrediction".news_score IS
    'Overnight macro news score in [-1, 1] for next_trade_date (window: previous trading day 15:30 IST to 08:30 IST).';
