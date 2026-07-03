-- Realised, forward-looking quality outcomes for auditing NIFTY predictions.
-- These columns must never be consumed as same-day strategy input features.

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS bull_score double precision,
    ADD COLUMN IF NOT EXISTS bear_score double precision,
    ADD COLUMN IF NOT EXISTS signal_quality double precision,
    ADD COLUMN IF NOT EXISTS quality_horizon_days integer;

COMMENT ON COLUMN "NiftyPrediction".bull_score IS
    '(max future high over quality horizon - signal close) / signal-date ATR14 SMA';
COMMENT ON COLUMN "NiftyPrediction".bear_score IS
    '(signal close - min future low over quality horizon) / signal-date ATR14 SMA';
COMMENT ON COLUMN "NiftyPrediction".signal_quality IS
    'Realised market direction: (bull_score - bear_score) / (bull_score + bear_score)';
COMMENT ON COLUMN "NiftyPrediction".quality_horizon_days IS
    'UNDERLYING_LOOKBACK_DAYS value used to calculate realised quality outcomes';
