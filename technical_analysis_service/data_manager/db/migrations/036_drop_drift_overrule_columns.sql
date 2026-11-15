-- Migration 036: remove legacy drift-overrule columns.
--
-- DRIFT_PROBE is now a normal production strategy. There is no post-prediction
-- drift override layer, so these stored override outputs are redundant.

ALTER TABLE "NiftyPrediction"
    DROP COLUMN IF EXISTS drift_effective_prediction,
    DROP COLUMN IF EXISTS drift_position_size_pct,
    DROP COLUMN IF EXISTS drift_overrule_reason;

ALTER TABLE "PaperExecutionSignal"
    DROP COLUMN IF EXISTS drift_position_size_pct;
