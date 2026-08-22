-- Migration 028: add alt_trade_label to NiftyPrediction
-- Reference price = signal-date close (close_1515) instead of next_open.
-- CALL if (next_high  - close_1515) / close_1515 >= target_threshold
-- PUT  if (close_1515 - next_low)   / close_1515 >= target_threshold

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS alt_trade_label varchar(20);

-- Backfill using the common threshold retained by the production strategy.
UPDATE "NiftyPrediction"
SET alt_trade_label = CASE
    WHEN next_high IS NULL OR next_low IS NULL OR close_1515 IS NULL
         OR close_1515 = 0
    THEN NULL
    WHEN (next_high  - close_1515) / close_1515 >= 0.010
         AND (close_1515 - next_low)   / close_1515 >= 0.010
    THEN 'BOTH'
    WHEN (next_high  - close_1515) / close_1515 >= 0.010
    THEN 'CALL'
    WHEN (close_1515 - next_low)   / close_1515 >= 0.010
    THEN 'PUT'
    ELSE 'NO_POSITION'
END
WHERE symbol = 'NIFTY'
  AND model_version = 'cascade_v1';

COMMENT ON COLUMN "NiftyPrediction".alt_trade_label IS
    'Alternative label using signal-date close as entry reference. '
    'CALL if (next_high - close_1515)/close_1515 >= target_threshold; '
    'PUT if (close_1515 - next_low)/close_1515 >= target_threshold.';
