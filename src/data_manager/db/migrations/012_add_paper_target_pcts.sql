ALTER TABLE "PaperExecutionSignal"
    ADD COLUMN IF NOT EXISTS target_1_pct double precision,
    ADD COLUMN IF NOT EXISTS target_2_pct double precision;

COMMENT ON COLUMN "PaperExecutionSignal".target_1_pct IS
    'First option-premium target as a decimal; absolute price is set from the actual paper fill.';

COMMENT ON COLUMN "PaperExecutionSignal".target_2_pct IS
    'Second option-premium target as a decimal; absolute price is set from the actual paper fill.';
