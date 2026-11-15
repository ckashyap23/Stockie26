ALTER TABLE "MacroFactorDaily"
    ADD COLUMN IF NOT EXISTS brent_close double precision,
    ADD COLUMN IF NOT EXISTS wti_close double precision;

UPDATE "MacroFactorDaily"
SET brent_close = crude_close
WHERE brent_close IS NULL AND crude_close IS NOT NULL;

COMMENT ON COLUMN "MacroFactorDaily".brent_close IS
    'Yahoo BZ=F daily close; rolling futures level, not spot.';
COMMENT ON COLUMN "MacroFactorDaily".wti_close IS
    'Yahoo CL=F daily close; rolling futures level, not spot.';