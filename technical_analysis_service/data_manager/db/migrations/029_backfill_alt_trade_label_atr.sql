-- Migration 029: recompute alt_trade_label with ATR-based dynamic threshold.
--
-- New definition (replaces migration-028 logic):
--   entry_ref = next_open  (same as actual_trade_label)
--   future_high_nd = MAX(high_day) over next 3 trading sessions  (UNDERLYING_LOOKBACK_DAYS=3)
--   future_low_nd  = MIN(low_day)  over next 3 trading sessions
--   target_pct = CLIP(0.55 * atr14 / close_1515, 0.004, 0.012)
--   CALL  if (future_high_nd - next_open) / next_open >= target_pct  AND  PUT condition false
--   PUT   if (next_open - future_low_nd)  / next_open >= target_pct  AND  CALL condition false
--   BOTH  if both
--   NO_POSITION otherwise
--
-- Requires: SignalFeatureDaily.atr14 and .high_day / .low_day are fully backfilled.

-- Step 1: ensure the column exists (idempotent).
ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS alt_trade_label varchar(20);

-- Step 2: backfill via CTE.
WITH
-- Rank the next 3 trading sessions for each signal_date.
ranked_fwd AS (
    SELECT
        base.signal_date  AS base_date,
        fwd.high_day,
        fwd.low_day,
        ROW_NUMBER() OVER (
            PARTITION BY base.signal_date
            ORDER BY fwd.signal_date
        ) AS rn
    FROM "SignalFeatureDaily" base
    JOIN "SignalFeatureDaily" fwd
        ON  fwd.symbol      = base.symbol
        AND fwd.signal_date > base.signal_date
    WHERE base.symbol = 'NIFTY'
),
-- Aggregate to future_high_nd / future_low_nd over the next 3 sessions.
future_ext AS (
    SELECT
        base_date,
        MAX(high_day) AS future_high_nd,
        MIN(low_day)  AS future_low_nd
    FROM ranked_fwd
    WHERE rn <= 3
    GROUP BY base_date
),
-- Pull atr14 and close_1515 from SignalFeatureDaily.
features AS (
    SELECT
        signal_date,
        atr14,
        close_1515
    FROM "SignalFeatureDaily"
    WHERE symbol = 'NIFTY'
      AND atr14 IS NOT NULL
      AND close_1515 IS NOT NULL
      AND close_1515 <> 0
),
-- Compute the per-row dynamic threshold and label.
computed AS (
    SELECT
        p.signal_date,
        GREATEST(0.004, LEAST(0.012, 0.55 * f.atr14 / f.close_1515)) AS target_pct,
        p.next_open,
        fe.future_high_nd,
        fe.future_low_nd
    FROM "NiftyPrediction" p
    JOIN features  f  ON f.signal_date  = p.signal_date
    JOIN future_ext fe ON fe.base_date  = p.signal_date
    WHERE p.symbol        = 'NIFTY'
      AND p.model_version = 'cascade_v1'
      AND p.next_open IS NOT NULL
      AND p.next_open <> 0
)
UPDATE "NiftyPrediction" np
SET alt_trade_label = CASE
    -- Both sides touched
    WHEN (c.future_high_nd - c.next_open) / c.next_open >= c.target_pct
         AND (c.next_open - c.future_low_nd) / c.next_open >= c.target_pct
    THEN 'BOTH'
    -- CALL only
    WHEN (c.future_high_nd - c.next_open) / c.next_open >= c.target_pct
    THEN 'CALL'
    -- PUT only
    WHEN (c.next_open - c.future_low_nd) / c.next_open >= c.target_pct
    THEN 'PUT'
    ELSE 'NO_POSITION'
END
FROM computed c
WHERE np.symbol        = 'NIFTY'
  AND np.model_version = 'cascade_v1'
  AND np.signal_date   = c.signal_date;

-- Step 3: null out rows where future data is not yet available
--         (pending / most-recent unresolved day — next_open IS NULL).
UPDATE "NiftyPrediction"
SET alt_trade_label = NULL
WHERE symbol        = 'NIFTY'
  AND model_version = 'cascade_v1'
  AND next_open IS NULL;

COMMENT ON COLUMN "NiftyPrediction".alt_trade_label IS
    'ATR-based dynamic label. Entry = next_open; look-ahead = UNDERLYING_LOOKBACK_DAYS (3) sessions. '
    'target_pct = CLIP(0.55 * atr14 / close_1515, 0.004, 0.012). '
    'CALL if (future_high_nd - next_open)/next_open >= target_pct; '
    'PUT if (next_open - future_low_nd)/next_open >= target_pct; BOTH if both; NO_POSITION otherwise.';
