ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS atr14_sma double precision;

COMMENT ON COLUMN "SignalFeatureDaily".atr14_sma IS
    'Simple moving average of true range over 14 trading sessions, including signal_date.';
