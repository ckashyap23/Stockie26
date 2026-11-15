-- Migration 035: remove legacy regime columns.
--
-- The cascade now uses one common strategy roster and one common threshold set
-- across all dates. These columns are no longer read or written by production,
-- research backtests, option selection, or paper trading.

DROP INDEX IF EXISTS ix_signal_feature_regime;

ALTER TABLE "SignalFeatureDaily"
    DROP COLUMN IF EXISTS regime;

ALTER TABLE "NiftyPrediction"
    DROP COLUMN IF EXISTS regime,
    DROP COLUMN IF EXISTS volatility_regime;

ALTER TABLE "NiftyOptionSelection"
    DROP COLUMN IF EXISTS volatility_regime;
