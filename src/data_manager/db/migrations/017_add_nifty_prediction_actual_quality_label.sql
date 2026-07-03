-- Realised CALL/PUT/NO_POSITION label derived from ATR-normalised quality scores.

ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS actual_quality_label varchar(20);

COMMENT ON COLUMN "NiftyPrediction".actual_quality_label IS
    'CALL when bull_score > 0.5 and raw direction > 0; PUT when bear_score > 0.5 and raw direction < 0; otherwise NO_POSITION';
