-- Migration 043: parallel relaxed directional outcome label.

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS actual_trade_label_relaxed varchar(20);

COMMENT ON COLUMN "NiftyPrediction".actual_trade_label_relaxed
    IS 'Directional CALL/PUT/BOTH/NO_POSITION outcome using NIFTY_TARGET_PCT_RELAXED.';
