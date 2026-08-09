-- Migration 026: add event_gate_reason to NiftyPrediction
-- Records which macro-event gate suppressed or demoted a trade-eligible prediction.
-- Format: "SUPPRESS:<family>" or "DEMOTE:<family>" or "" when no gate fired.

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS event_gate_reason varchar(80);

COMMENT ON COLUMN "NiftyPrediction".event_gate_reason IS
    'Set by the event-calendar gate: SUPPRESS:<family> (no trade, no watch) or DEMOTE:<family> (watch only). Empty when no gate fired.';
