ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS watch_strategy_type varchar(40),
    ADD COLUMN IF NOT EXISTS prior_watch_strategy_type varchar(40),
    ADD COLUMN IF NOT EXISTS confirming_strategy_type varchar(40);

COMMENT ON COLUMN "NiftyPrediction".primary_strategy_type IS
    'Canonical policy: TRADE_ELIGIBLE, WATCH_ONLY, or RESEARCH.';

UPDATE "NiftyPrediction"
SET primary_strategy_type = CASE
    WHEN primary_strategy IN (
        'OversoldBounceCall_MoreTrades', 'DownMomentumPut_MoreTrades',
        'RangeBreakoutPut', 'UpMomentumCall_HighPrecision'
    ) THEN 'WATCH_ONLY'
    WHEN primary_strategy_type = 'PRODUCTION_TRADE_ELIGIBLE' THEN 'TRADE_ELIGIBLE'
    WHEN primary_strategy_type = 'PRODUCTION_WATCH_ONLY' THEN 'WATCH_ONLY'
    ELSE primary_strategy_type
END;

UPDATE "NiftyPrediction"
SET watch_strategy_type = CASE
        WHEN watch_variant IN (
            'OversoldBounceCall_MoreTrades', 'DownMomentumPut_MoreTrades',
            'RangeBreakoutPut', 'UpMomentumCall_HighPrecision'
        ) THEN 'WATCH_ONLY'
        ELSE 'TRADE_ELIGIBLE'
    END
WHERE watch_variant IS NOT NULL;

UPDATE "NiftyPrediction"
SET prior_watch_strategy_type = CASE
        WHEN prior_watch_variant IN (
            'OversoldBounceCall_MoreTrades', 'DownMomentumPut_MoreTrades',
            'RangeBreakoutPut', 'UpMomentumCall_HighPrecision'
        ) THEN 'WATCH_ONLY'
        ELSE 'TRADE_ELIGIBLE'
    END
WHERE prior_watch_variant IS NOT NULL;

UPDATE "NiftyPrediction"
SET confirming_strategy_type = 'TRADE_ELIGIBLE'
WHERE confirming_variant IS NOT NULL;

ALTER TABLE "NiftyPrediction"
    DROP COLUMN IF EXISTS primary_strategy_authority,
    DROP COLUMN IF EXISTS primary_strategy_role,
    DROP COLUMN IF EXISTS watch_authority;
