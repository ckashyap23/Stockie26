-- Migration 042: robust FII index-futures positioning component.

ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS fii_idx_5d_z double precision,
    ADD COLUMN IF NOT EXISTS fii_net_1d_cr double precision;

COMMENT ON COLUMN "SignalFeatureDaily".fii_idx_5d_z
    IS 'Negated robust z-score of the five-session FII index-futures long-ratio change, based on shifted trailing history and clipped to +/-3.';

COMMENT ON COLUMN "SignalFeatureDaily".fii_net_5d_z
    IS 'Diagnostic cash-flow feature only; not used by macro_risk_off.';
COMMENT ON COLUMN "SignalFeatureDaily".fii_net_1d_cr
    IS 'Raw daily provisional FII cash net flow, retained for diagnostics only.';

COMMENT ON COLUMN "SignalFeatureDaily".macro_risk_off
    IS 'Weighted score of crude (0.25), US 10Y (0.50), and USD/INR (0.25) z-scores, renormalized over available inputs.';
