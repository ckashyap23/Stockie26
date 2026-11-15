-- Add stop_loss_pct to PaperExecutionSignal so the percentage is stored
-- alongside the absolute price, and the ratchet logic can recompute from it.
ALTER TABLE "PaperExecutionSignal"
    ADD COLUMN IF NOT EXISTS stop_loss_pct double precision;

COMMENT ON COLUMN "PaperExecutionSignal".stop_loss_pct IS
    'Stop-loss as a decimal fraction of entry (e.g. 0.03 = 3%); absolute price is computed at open time.';
