-- Migration 028: add alt_trade_label to NiftyPrediction
-- Reference price = signal-date close (close_1515) instead of next_open.
-- CALL if (next_high  - close_1515) / close_1515 >= regime_threshold
-- PUT  if (close_1515 - next_low)   / close_1515 >= regime_threshold

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS alt_trade_label varchar(20);

-- Backfill using regime-aware thresholds (stress=0.5%, calm=0.3%)
UPDATE "NiftyPrediction"
SET alt_trade_label = CASE
    WHEN next_high IS NULL OR next_low IS NULL OR close_1515 IS NULL
         OR close_1515 = 0
    THEN NULL
    -- stress regime (0.5% threshold)
    WHEN regime = 'stress'
         AND (next_high  - close_1515) / close_1515 >= 0.005
         AND (close_1515 - next_low)   / close_1515 >= 0.005
    THEN 'BOTH'
    WHEN regime = 'stress'
         AND (next_high  - close_1515) / close_1515 >= 0.005
    THEN 'CALL'
    WHEN regime = 'stress'
         AND (close_1515 - next_low)   / close_1515 >= 0.005
    THEN 'PUT'
    -- calm regime (0.3% threshold)
    WHEN regime = 'calm'
         AND (next_high  - close_1515) / close_1515 >= 0.003
         AND (close_1515 - next_low)   / close_1515 >= 0.003
    THEN 'BOTH'
    WHEN regime = 'calm'
         AND (next_high  - close_1515) / close_1515 >= 0.003
    THEN 'CALL'
    WHEN regime = 'calm'
         AND (close_1515 - next_low)   / close_1515 >= 0.003
    THEN 'PUT'
    ELSE 'NO_POSITION'
END
WHERE symbol = 'NIFTY'
  AND model_version = 'cascade_v1';

COMMENT ON COLUMN "NiftyPrediction".alt_trade_label IS
    'Alternative label using signal-date close as entry reference. '
    'CALL if (next_high - close_1515)/close_1515 >= regime_threshold; '
    'PUT if (close_1515 - next_low)/close_1515 >= regime_threshold.';
