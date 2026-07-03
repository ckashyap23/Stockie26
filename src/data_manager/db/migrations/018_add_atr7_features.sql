-- Seven-session ATR variants, parallel to atr14 and atr14_sma.

ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS atr7 double precision,
    ADD COLUMN IF NOT EXISTS atr7_sma double precision;

COMMENT ON COLUMN "SignalFeatureDaily".atr7 IS
    'Wilder-style exponentially smoothed true range with period 7';
COMMENT ON COLUMN "SignalFeatureDaily".atr7_sma IS
    'Simple moving average of true range over 7 trading sessions, including signal_date';
