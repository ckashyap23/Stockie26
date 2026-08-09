from __future__ import annotations

import pandas as pd

from src.technical_analysis.prediction.features import compute_underlying_features


def test_validated_support_resistance_features_use_prior_10_completed_sessions() -> None:
    df = pd.DataFrame(
        {
            "close_price": [100.2, 109.8, 101.0, 109.9, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 109.8],
            "high_price": [105.0, 110.0, 103.0, 110.1, 104.5, 105.5, 106.5, 107.5, 108.5, 109.5, 111.0],
            "low_price": [100.0, 105.0, 100.1, 106.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 108.8],
            "volume": [100_000] * 11,
        }
    )

    features = compute_underlying_features(df)

    assert features["support_level_10d"] == 100.0
    assert features["resistance_level_10d"] == 110.1
    assert features["support_distance_10d"] == round((109.8 - 100.0) / 109.8, 6)
    assert features["resistance_distance_10d"] == round((110.1 - 109.8) / 109.8, 6)
    assert features["support_bounce_count_10d"] == 2
    assert features["resistance_rejection_count_10d"] == 2
    assert features["support_broken_10d"] is False
    assert features["resistance_broken_10d"] is False
    assert features["near_validated_support_10d"] is False
    assert features["near_validated_resistance_10d"] is True
    assert features["room_to_validated_resistance_10d"] == round((110.1 - 109.8) / 109.8, 6)


def test_validated_resistance_room_blank_when_resistance_is_broken() -> None:
    df = pd.DataFrame(
        {
            "close_price": [100.2, 109.8, 101.0, 109.9, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 111.0],
            "high_price": [105.0, 110.0, 103.0, 110.1, 104.5, 105.5, 106.5, 107.5, 108.5, 109.5, 112.0],
            "low_price": [100.0, 105.0, 100.1, 106.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.8],
            "volume": [100_000] * 11,
        }
    )

    features = compute_underlying_features(df)

    assert features["resistance_broken_10d"] is True
    assert features["near_validated_resistance_10d"] is False
    assert features["room_to_validated_resistance_10d"] is None


def test_resistance_broken_consistent_with_distance_going_negative() -> None:
    """resistance_broken_10d must fire exactly when resistance_distance_10d goes negative.

    Previously a 0.2% margin on resistance_broken caused a contradictory zone where
    distance < 0 (close above resistance) but broken = False.  The margin is removed
    so the two features are always in agreement.
    """
    # close = 110.15: just above resistance 110.1 but within the old 0.2% margin
    df = pd.DataFrame(
        {
            "close_price": [100.2, 109.8, 101.0, 109.9, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.15],
            "high_price":  [105.0, 110.0, 103.0, 110.1, 104.5, 105.5, 106.5, 107.5, 108.5, 109.5, 111.0],
            "low_price":   [100.0, 105.0, 100.1, 106.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.5],
            "volume": [100_000] * 11,
        }
    )
    features = compute_underlying_features(df)
    # distance is negative (close > prior resistance)
    assert features["resistance_distance_10d"] is not None
    assert features["resistance_distance_10d"] < 0
    # broken flag must agree — was False (bug) under the old 0.2% margin
    assert features["resistance_broken_10d"] is True
