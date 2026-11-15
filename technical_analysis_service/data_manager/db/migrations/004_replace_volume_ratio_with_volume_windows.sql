ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS volume_10d double precision,
    ADD COLUMN IF NOT EXISTS volume_20d double precision,
    DROP COLUMN IF EXISTS volume_ratio;
