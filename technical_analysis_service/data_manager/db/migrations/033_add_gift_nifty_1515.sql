-- Migration 033: add gift_1515 to GiftNiftySnapshot; relax NOT NULL on open candle columns
--
-- gift_1515: GIFT NIFTY close price of the 3:10-3:15 PM IST 5m candle (captured when
-- India's cash market closes). Used as the D-1 reference in the overnight gap formula:
--   gift_gap_pct(D) = gift_920(D) / gift_1515(D-1) - 1
--
-- The existing columns become nullable so the 9:20 AM (open) and 3:15 PM (close)
-- snapshots can be written by two separate cron jobs without requiring both at INSERT time.

ALTER TABLE "GiftNiftySnapshot"
    ADD COLUMN IF NOT EXISTS gift_1515 double precision,
    ALTER COLUMN open_915  DROP NOT NULL,
    ALTER COLUMN high_920  DROP NOT NULL,
    ALTER COLUMN low_920   DROP NOT NULL,
    ALTER COLUMN close_920 DROP NOT NULL;

COMMENT ON COLUMN "GiftNiftySnapshot".gift_1515 IS
    'GIFT NIFTY close price at 3:15 PM IST (3:10 candle close). '
    'Used as the D-1 reference for gift_gap_pct = gift_920(D) / gift_1515(D-1) - 1.';
