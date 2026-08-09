ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS support_level_10d double precision,
    ADD COLUMN IF NOT EXISTS resistance_level_10d double precision,
    ADD COLUMN IF NOT EXISTS support_distance_10d double precision,
    ADD COLUMN IF NOT EXISTS support_bounce_count_10d integer,
    ADD COLUMN IF NOT EXISTS resistance_rejection_count_10d integer,
    ADD COLUMN IF NOT EXISTS support_broken_10d boolean,
    ADD COLUMN IF NOT EXISTS resistance_broken_10d boolean,
    ADD COLUMN IF NOT EXISTS near_validated_support_10d boolean,
    ADD COLUMN IF NOT EXISTS near_validated_resistance_10d boolean,
    ADD COLUMN IF NOT EXISTS room_to_validated_resistance_10d double precision;

COMMENT ON COLUMN "SignalFeatureDaily".support_level_10d IS
    'Lowest support candidate from the prior 10 completed sessions, excluding the current signal day.';
COMMENT ON COLUMN "SignalFeatureDaily".resistance_level_10d IS
    'Highest high from the prior 10 completed sessions, excluding the current signal day.';
COMMENT ON COLUMN "SignalFeatureDaily".support_distance_10d IS
    '(close_1515 - support_level_10d) / close_1515.';
COMMENT ON COLUMN "SignalFeatureDaily".resistance_distance_10d IS
    '(resistance_level_10d - close_1515) / close_1515.';
COMMENT ON COLUMN "SignalFeatureDaily".support_bounce_count_10d IS
    'Prior-10-session count where low_day <= support_level_10d * 1.0025 and close_1515 >= support_level_10d * 1.001.';
COMMENT ON COLUMN "SignalFeatureDaily".resistance_rejection_count_10d IS
    'Prior-10-session count where high_day >= resistance_level_10d * 0.9975 and close_1515 <= resistance_level_10d * 0.999.';
COMMENT ON COLUMN "SignalFeatureDaily".support_broken_10d IS
    'True when close_1515 < support_level_10d (close has crossed below the 10-day support).';
COMMENT ON COLUMN "SignalFeatureDaily".resistance_broken_10d IS
    'True when close_1515 > resistance_level_10d (close has crossed above the 10-day resistance).';
COMMENT ON COLUMN "SignalFeatureDaily".near_validated_support_10d IS
    'True when close_1515 is near support, support has at least two prior bounces, and support is not broken.';
COMMENT ON COLUMN "SignalFeatureDaily".near_validated_resistance_10d IS
    'True when close_1515 is near resistance, resistance has at least two prior rejections, and resistance is not broken.';
COMMENT ON COLUMN "SignalFeatureDaily".room_to_validated_resistance_10d IS
    'Resistance room populated only when the 10-day resistance has at least two prior rejections and is not broken.';
