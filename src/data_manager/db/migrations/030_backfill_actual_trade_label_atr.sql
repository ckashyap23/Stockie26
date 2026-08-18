-- Migration 030: replace actual_trade_label with ATR-based dynamic threshold.
--
-- New definition (replaces the earlier fixed env-var thresholds):
--   entry_ref  = next_open  (unchanged)
--   look-ahead = MAX(high_day) / MIN(low_day) over next 3 trading sessions
--   target_pct = CLIP(0.55 * atr14 / close_1515, 0.004, 0.012)  per row
--
--   CALL  if (future_high_nd - next_open) / next_open >= target_pct  AND PUT false
--   PUT   if (next_open - future_low_nd)  / next_open >= target_pct  AND CALL false
--   BOTH  if both conditions met
--   NO_POSITION otherwise
--
-- Requires: SignalFeatureDaily.atr14 and high_day / low_day fully backfilled.

-- Step 1: build future extremes over next 3 trading sessions per signal_date
WITH
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
future_ext AS (
    SELECT
        base_date,
        MAX(high_day) AS future_high_nd,
        MIN(low_day)  AS future_low_nd
    FROM ranked_fwd
    WHERE rn <= 3
    GROUP BY base_date
),
features AS (
    SELECT signal_date, atr14, close_1515
    FROM "SignalFeatureDaily"
    WHERE symbol = 'NIFTY'
      AND atr14 IS NOT NULL
      AND close_1515 IS NOT NULL
      AND close_1515 <> 0
),
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
SET actual_trade_label = CASE
    WHEN (c.future_high_nd - c.next_open) / c.next_open >= c.target_pct
         AND (c.next_open - c.future_low_nd) / c.next_open >= c.target_pct
    THEN 'BOTH'
    WHEN (c.future_high_nd - c.next_open) / c.next_open >= c.target_pct
    THEN 'CALL'
    WHEN (c.next_open - c.future_low_nd) / c.next_open >= c.target_pct
    THEN 'PUT'
    ELSE 'NO_POSITION'
END
FROM computed c
WHERE np.symbol        = 'NIFTY'
  AND np.model_version = 'cascade_v1'
  AND np.signal_date   = c.signal_date;

-- Step 2: null out unresolved rows (no future data yet)
UPDATE "NiftyPrediction"
SET actual_trade_label = NULL
WHERE symbol        = 'NIFTY'
  AND model_version = 'cascade_v1'
  AND next_open IS NULL;

COMMENT ON COLUMN "NiftyPrediction".actual_trade_label IS
    'ATR-based dynamic label (migration 030). Entry = next_open; '
    'look-ahead = 3 trading sessions. '
    'target_pct = CLIP(0.55 * atr14 / close_1515, 0.004, 0.012). '
    'CALL / PUT / BOTH / NO_POSITION.';
