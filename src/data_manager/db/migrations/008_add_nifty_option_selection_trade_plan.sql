-- Add actionable target/stop levels to persisted NIFTY option selections.

ALTER TABLE "NiftyOptionSelection"
    ADD COLUMN IF NOT EXISTS target_1_pct double precision,
    ADD COLUMN IF NOT EXISTS target_1_price double precision,
    ADD COLUMN IF NOT EXISTS target_2_pct double precision,
    ADD COLUMN IF NOT EXISTS target_2_price double precision,
    ADD COLUMN IF NOT EXISTS stop_loss_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS stop_loss_pct double precision,
    ADD COLUMN IF NOT EXISTS stop_loss_price double precision;