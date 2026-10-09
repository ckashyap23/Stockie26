-- Migration 040: point-in-time macro inputs and MacroDirection features.

CREATE TABLE IF NOT EXISTS "MacroFactorDaily" (
    factor_date date PRIMARY KEY,
    india_vix double precision,
    source_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz
);

ALTER TABLE "MacroFactorDaily"
    ADD COLUMN IF NOT EXISTS crude_close double precision,
    ADD COLUMN IF NOT EXISTS us10y_level double precision,
    ADD COLUMN IF NOT EXISTS usdinr_close double precision,
    ADD COLUMN IF NOT EXISTS fii_net_cr double precision,
    ADD COLUMN IF NOT EXISTS dii_net_cr double precision;

ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS crude_ret_1d double precision,
    ADD COLUMN IF NOT EXISTS crude_ret_5d double precision,
    ADD COLUMN IF NOT EXISTS us10y_chg_1d double precision,
    ADD COLUMN IF NOT EXISTS us10y_chg_5d double precision,
    ADD COLUMN IF NOT EXISTS us10y_level double precision,
    ADD COLUMN IF NOT EXISTS usdinr_ret_1d double precision,
    ADD COLUMN IF NOT EXISTS usdinr_ret_5d double precision,
    ADD COLUMN IF NOT EXISTS fii_net_cr double precision,
    ADD COLUMN IF NOT EXISTS fii_net_5d_cr double precision,
    ADD COLUMN IF NOT EXISTS dii_net_cr double precision,
    ADD COLUMN IF NOT EXISTS crude_ret_5d_z double precision,
    ADD COLUMN IF NOT EXISTS us10y_chg_5d_z double precision,
    ADD COLUMN IF NOT EXISTS usdinr_ret_5d_z double precision,
    ADD COLUMN IF NOT EXISTS fii_net_5d_z double precision,
    ADD COLUMN IF NOT EXISTS macro_risk_off double precision;

COMMENT ON COLUMN "MacroFactorDaily".factor_date
    IS 'Signal date D. Values are loaded only after they are available before the D+1 NSE open.';
COMMENT ON COLUMN "SignalFeatureDaily".us10y_chg_5d
    IS 'Five-session change in the US 10Y yield, in basis points.';
COMMENT ON COLUMN "SignalFeatureDaily".macro_risk_off
    IS 'Equal-weight mean of available trailing macro z-scores; FII z-score enters with a negative sign.';
