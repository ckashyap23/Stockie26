ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS primary_strategy_family varchar(80),
    ADD COLUMN IF NOT EXISTS primary_strategy_type varchar(40),
    ADD COLUMN IF NOT EXISTS primary_strategy_authority varchar(40),
    ADD COLUMN IF NOT EXISTS primary_strategy_role varchar(40),
    ADD COLUMN IF NOT EXISTS watch_family varchar(80),
    ADD COLUMN IF NOT EXISTS watch_variant varchar(120),
    ADD COLUMN IF NOT EXISTS watch_authority varchar(40),
    ADD COLUMN IF NOT EXISTS prior_watch_family varchar(80),
    ADD COLUMN IF NOT EXISTS prior_watch_variant varchar(120),
    ADD COLUMN IF NOT EXISTS confirming_family varchar(80),
    ADD COLUMN IF NOT EXISTS confirming_variant varchar(120),
    ADD COLUMN IF NOT EXISTS family_confirmation_match boolean,
    ADD COLUMN IF NOT EXISTS promotion_block_reason varchar(120);
