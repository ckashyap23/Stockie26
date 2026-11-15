-- Migration 048: add gift_0910 to GiftNiftySnapshot
--
-- gift_0910: GIFT NIFTY close of the 9:05-9:10 AM IST 5m candle, i.e. the last
-- completed price before the 09:12 pre-open decision. Unlike close_920 (the
-- 9:15-9:20 candle close, which only exists after the cash open) it is available
-- before the decision, and in the 2024-2026 backfill it explains the NIFTY
-- opening gap better (R2 ~0.81 vs ~0.70).
--   gift_gap_0910(D) = gift_0910(D) / gift_1515(D-1) - 1

ALTER TABLE "GiftNiftySnapshot"
    ADD COLUMN IF NOT EXISTS gift_0910 double precision;

COMMENT ON COLUMN "GiftNiftySnapshot".gift_0910 IS
    'GIFT NIFTY close price at 9:10 AM IST (9:05 candle close); pre-open decision input.';
