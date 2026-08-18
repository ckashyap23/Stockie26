-- Migration 037: persist guard-layer gap suppressions.

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS gap_gate_reason varchar(80);

COMMENT ON COLUMN "NiftyPrediction".gap_gate_reason IS
    'Guard-layer reason when effective_prediction is suppressed by the opening gap guard.';
