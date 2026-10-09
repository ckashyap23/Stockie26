-- Migration 041: point-in-time NSE participant-wise FII index-futures positioning.

ALTER TABLE "MacroFactorDaily"
    ADD COLUMN IF NOT EXISTS fii_idx_fut_long bigint,
    ADD COLUMN IF NOT EXISTS fii_idx_fut_short bigint;

ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS fii_idx_long_ratio double precision,
    ADD COLUMN IF NOT EXISTS fii_idx_long_ratio_chg5 double precision;

COMMENT ON COLUMN "MacroFactorDaily".fii_idx_fut_long
    IS 'FII Future Index Long contracts from the NSE participant-wise OI archive for signal date D.';
COMMENT ON COLUMN "MacroFactorDaily".fii_idx_fut_short
    IS 'FII Future Index Short contracts from the NSE participant-wise OI archive for signal date D.';
COMMENT ON COLUMN "SignalFeatureDaily".fii_idx_long_ratio
    IS 'FII index-futures long contracts divided by total FII index-futures long and short contracts.';
COMMENT ON COLUMN "SignalFeatureDaily".fii_idx_long_ratio_chg5
    IS 'Five-session difference in fii_idx_long_ratio using available participant-OI sessions.';
