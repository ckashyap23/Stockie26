-- Migration 034: add drift_overrule columns to NiftyPrediction
--
-- drift_overrule is a thin post-cascade layer applied at 9:22 AM IST inside
-- daily_nifty_signal.py after the open-gap features are available.
-- It preserves the original cascade output (effective_prediction) and stores
-- the overruled direction in drift_effective_prediction for option selection.

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS drift_effective_prediction varchar(20),
    ADD COLUMN IF NOT EXISTS drift_position_size_pct    double precision,
    ADD COLUMN IF NOT EXISTS drift_overrule_reason      varchar(120);

COMMENT ON COLUMN "NiftyPrediction".drift_effective_prediction IS
    'Direction after drift overrule: CALL / PUT / NO_POSITION. Null = not yet computed.';
COMMENT ON COLUMN "NiftyPrediction".drift_position_size_pct IS
    'Position size as fraction of capital after drift override (e.g. 0.5 = half size).';
COMMENT ON COLUMN "NiftyPrediction".drift_overrule_reason IS
    'Reason code: DRIFT_CONFIRMS_HALF_SIZE | DRIFT_CONFIRMS_FULL | DRIFT_OPPOSES '
    '| DRIFT_PROBE | TAIL_SHOCK | DRIFT_PROMOTES_WATCH | NO_CHANGE | DRIFT_NONE.';
