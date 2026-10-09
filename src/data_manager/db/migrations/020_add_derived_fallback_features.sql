ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS volume_hybrid double precision,
    ADD COLUMN IF NOT EXISTS resistance_distance_10d double precision;

UPDATE "SignalFeatureDaily"
SET
    volume_hybrid = CASE
        WHEN volume_20d IS NOT NULL AND volume_20d <> 0 AND volume_day IS NOT NULL
        THEN volume_day / volume_20d
        ELSE volume_hybrid
    END,
    resistance_distance_10d = CASE
        WHEN recent_high_10d IS NOT NULL AND close_1515 IS NOT NULL AND close_1515 <> 0
        THEN (recent_high_10d - close_1515) / close_1515
        ELSE resistance_distance_10d
    END;
