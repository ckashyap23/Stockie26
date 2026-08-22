ALTER TABLE "NiftyPrediction"
    DROP COLUMN IF EXISTS watch_signal,
    DROP COLUMN IF EXISTS prior_watch_signal,
    DROP COLUMN IF EXISTS prior_watch_age,
    DROP COLUMN IF EXISTS promoted_prediction,
    DROP COLUMN IF EXISTS promotion_reason,
    DROP COLUMN IF EXISTS watch_family,
    DROP COLUMN IF EXISTS watch_variant,
    DROP COLUMN IF EXISTS watch_strategy_type,
    DROP COLUMN IF EXISTS prior_watch_family,
    DROP COLUMN IF EXISTS prior_watch_variant,
    DROP COLUMN IF EXISTS prior_watch_strategy_type,
    DROP COLUMN IF EXISTS confirming_family,
    DROP COLUMN IF EXISTS confirming_variant,
    DROP COLUMN IF EXISTS confirming_strategy_type,
    DROP COLUMN IF EXISTS family_confirmation_match,
    DROP COLUMN IF EXISTS promotion_block_reason,
    DROP COLUMN IF EXISTS position_size_pct;

ALTER TABLE "PaperExecutionSignal"
    DROP COLUMN IF EXISTS promoted_prediction,
    DROP COLUMN IF EXISTS entry_action,
    DROP COLUMN IF EXISTS opening_gap_pct,
    DROP COLUMN IF EXISTS call_reclaim_level;
