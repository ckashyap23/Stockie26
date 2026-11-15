-- Migration 032: add 9:15 AM open-gap features to SignalFeatureDaily
--
-- All features are stored on signal_date = D-1 (the signal row whose trade
-- executes on trade_date = D):
--
--   nifty_gap_pct   = nifty_open_915(D)  / close_1515(D-1) - 1
--   nifty_drift_pct = nifty_close_920(D) / nifty_open_915(D) - 1   (first 5-min tape)
--   gift_gap_pct    = gift_nifty_920(D)  / close_1515(D-1) - 1
--   gap_confirmed   = sign(nifty_gap) == sign(gift_gap) AND gap != 0
--   gap_fade        = gap != 0 AND drift != 0 AND sign(drift) != sign(gap)
--   gap_open_atr    = nifty_gap_pct / (atr14 / close_1515)
--   gift_gap_atr    = gift_gap_pct  / (atr14 / close_1515)

ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS nifty_gap_pct    double precision,
    ADD COLUMN IF NOT EXISTS nifty_drift_pct  double precision,
    ADD COLUMN IF NOT EXISTS gift_gap_pct     double precision,
    ADD COLUMN IF NOT EXISTS gap_confirmed    boolean,
    ADD COLUMN IF NOT EXISTS gap_fade         boolean,
    ADD COLUMN IF NOT EXISTS gap_open_atr     double precision,
    ADD COLUMN IF NOT EXISTS gift_gap_atr     double precision;

COMMENT ON COLUMN "SignalFeatureDaily".nifty_gap_pct   IS 'nifty_open_915(T)/close_1515(T-1)-1. Gap size as fraction.';
COMMENT ON COLUMN "SignalFeatureDaily".nifty_drift_pct IS 'nifty_close_920(T)/nifty_open_915(T)-1. First 5-min tape direction.';
COMMENT ON COLUMN "SignalFeatureDaily".gift_gap_pct    IS 'gift_nifty_920(T)/close_1515(T-1)-1. GIFT NIFTY gap vs NIFTY prev close.';
COMMENT ON COLUMN "SignalFeatureDaily".gap_confirmed   IS 'True when sign(nifty_gap)==sign(gift_gap) and gap!=0.';
COMMENT ON COLUMN "SignalFeatureDaily".gap_fade        IS 'True when first-5min drift opposes the opening gap (gap!=0 and drift!=0).';
COMMENT ON COLUMN "SignalFeatureDaily".gap_open_atr    IS 'nifty_gap_pct / (atr14/close_1515). Gap in ATR units.';
COMMENT ON COLUMN "SignalFeatureDaily".gift_gap_atr    IS 'gift_gap_pct / (atr14/close_1515). GIFT gap in ATR units.';
