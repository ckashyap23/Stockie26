from __future__ import annotations

import pandas as pd

from src.technical_analysis.prediction.features import (
    compute_candle_pattern_flag,
    compute_pattern_at_key_level,
    compute_rsi_divergence_flag,
    compute_obv,
    compute_swing_structure_state,
    compute_underlying_features,
)


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
    assert features["support_distance_10d"] is None
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


def test_swing_structure_state_detects_uptrend_intact() -> None:
    highs = [10, 12, 15, 13, 11, 12, 17, 14, 12, 13, 14]
    lows = [9, 10, 12, 11, 10, 11, 13, 12, 11, 12, 13]
    closes = [9.5, 11.5, 14.5, 12.5, 10.5, 11.5, 16.5, 13.5, 11.5, 12.5, 13.5]
    df = pd.DataFrame(
        {
            "close_price": closes,
            "high_price": highs,
            "low_price": lows,
            "volume": [100_000] * len(closes),
        }
    )

    features = compute_underlying_features(df)
    assert features["swing_structure_state"] == "uptrend_intact"


def test_swing_structure_state_detects_consolidation_from_compression() -> None:
    highs = pd.Series([10, 12, 16, 12, 11, 13, 15, 13, 12, 12, 11, 10])
    lows = pd.Series([9, 8, 10, 7, 6, 8, 9, 8, 7, 8, 9, 10])

    state = compute_swing_structure_state(highs, lows, close=8.0)
    assert state == "consolidation"


def test_rsi_divergence_flag_detects_bearish_divergence() -> None:
    closes = pd.Series([100, 102, 105, 103, 101, 103, 107, 104, 102])
    # Fractal highs at indices 2 and 6 (105, 107): higher high on price.
    rsi14 = pd.Series([50, 55, 72, 60, 52, 58, 66, 57, 53])

    flag = compute_rsi_divergence_flag(closes, rsi14)
    assert flag == "bearish"


def test_rsi_divergence_flag_carries_forward_on_non_event_day() -> None:
    closes = pd.Series([100, 102, 105, 103, 101, 103, 107, 104, 102, 101])
    # Swing highs at 2 and 6, second high confirms on day index 8; day 9 is non-event.
    rsi14 = pd.Series([50, 55, 72, 60, 52, 58, 66, 57, 53, 51])

    flag = compute_rsi_divergence_flag(closes, rsi14)
    assert flag == "bearish"


def test_rsi_divergence_flag_is_null_before_first_confirmed_swing() -> None:
    closes = pd.Series([100, 101, 102, 103, 104, 105])
    rsi14 = pd.Series([45, 46, 47, 48, 49, 50])

    flag = compute_rsi_divergence_flag(closes, rsi14)
    assert flag is None


def test_candle_pattern_flag_detects_hammer() -> None:
    opens = pd.Series([101.0, 100.0])
    highs = pd.Series([102.0, 101.2])
    lows = pd.Series([99.5, 97.8])
    closes = pd.Series([100.5, 101.0])

    flag = compute_candle_pattern_flag(opens, highs, lows, closes)
    assert flag == "hammer"


def test_candle_pattern_flag_detects_bearish_engulf() -> None:
    opens = pd.Series([100.0, 103.0])
    highs = pd.Series([101.0, 103.5])
    lows = pd.Series([99.0, 96.5])
    closes = pd.Series([102.0, 97.0])

    flag = compute_candle_pattern_flag(opens, highs, lows, closes)
    assert flag == "bearish_engulf"


def test_candle_pattern_flag_requires_opposite_prior_day_for_engulf() -> None:
    # Prior day is also up (close > open), so bullish engulf should not qualify.
    opens = pd.Series([100.0, 99.8])
    highs = pd.Series([102.0, 103.0])
    lows = pd.Series([99.5, 99.0])
    closes = pd.Series([101.0, 102.5])

    flag = compute_candle_pattern_flag(opens, highs, lows, closes)
    assert flag != "bullish_engulf"


def test_pattern_at_key_level_gates_flag() -> None:
    assert compute_pattern_at_key_level("hammer", True, False) == "hammer"
    assert compute_pattern_at_key_level("shooting_star", False, True) == "shooting_star"
    assert compute_pattern_at_key_level("doji", False, False) == "none"


def test_underlying_features_default_pattern_at_key_level_none() -> None:
    df = pd.DataFrame(
        {
            "open_price": [100.0, 101.0, 102.0],
            "high_price": [101.0, 102.0, 103.0],
            "low_price": [99.0, 100.0, 101.0],
            "close_price": [100.5, 101.5, 102.5],
            "volume": [100_000, 100_500, 101_000],
        }
    )

    features = compute_underlying_features(df)
    assert features["candle_pattern_flag"] in {
        "hammer",
        "shooting_star",
        "doji",
        "bullish_engulf",
        "bearish_engulf",
        "none",
    }
    assert features["pattern_at_key_level"] == "none"


def test_obv_and_obv_slope_10d_follow_spec() -> None:
    closes = pd.Series([100 + i for i in range(12)], dtype=float)
    volumes = pd.Series([100.0] * 12, dtype=float)
    obv = compute_obv(closes, volumes)

    assert obv.iloc[-1] == 1100.0
    assert (obv.iloc[-1] - obv.iloc[-11]) / 10.0 == 100.0

    df = pd.DataFrame(
        {
            "open_price": [99.5 + i for i in range(12)],
            "high_price": [100.5 + i for i in range(12)],
            "low_price": [98.5 + i for i in range(12)],
            "close_price": closes,
            "volume": volumes,
        }
    )
    features = compute_underlying_features(df)
    assert features["obv"] == 1100.0
    assert features["obv_slope_10d"] == 100.0


def test_banknifty_relative_strength_and_5d_mean_follow_spec() -> None:
    nifty_closes = pd.Series([100.0, 102.0, 101.0, 103.0, 104.0, 105.0])
    bank_closes = pd.Series([200.0, 206.0, 202.0, 208.0, 210.0, 215.0])

    df = pd.DataFrame(
        {
            "trade_date": pd.date_range("2026-01-01", periods=6, freq="D"),
            "open_price": [99.5, 101.5, 100.5, 102.5, 103.5, 104.5],
            "high_price": [100.5, 102.5, 101.5, 103.5, 104.5, 105.5],
            "low_price": [98.5, 100.5, 99.5, 101.5, 102.5, 103.5],
            "close_price": nifty_closes,
            "volume": [100_000] * 6,
        }
    ).set_index("trade_date", drop=False)

    bank_df = pd.DataFrame(
        {
            "trade_date": pd.date_range("2026-01-01", periods=6, freq="D"),
            "close_price": bank_closes,
        }
    ).set_index("trade_date", drop=False)

    features = compute_underlying_features(
        df,
        banknifty_window=bank_df,
        breadth_advance_decline_ratio=1.25,
    )

    nifty_ret = nifty_closes.pct_change()
    bank_ret = bank_closes.pct_change()
    rel = (bank_ret - nifty_ret).dropna()

    assert features["banknifty_rel_strength"] == round(float(rel.iloc[-1]), 6)
    assert features["banknifty_rel_strength_5d"] == round(float(rel.tail(5).mean()), 6)
    assert features["breadth_advance_decline_ratio"] == 1.25


def test_breadth_new_high_ratio_passthrough() -> None:
    df = pd.DataFrame(
        {
            "trade_date": pd.date_range("2026-01-01", periods=6, freq="D"),
            "open_price": [99.5, 101.5, 100.5, 102.5, 103.5, 104.5],
            "high_price": [100.5, 102.5, 101.5, 103.5, 104.5, 105.5],
            "low_price": [98.5, 100.5, 99.5, 101.5, 102.5, 103.5],
            "close_price": [100.0, 102.0, 101.0, 103.0, 104.0, 105.0],
            "volume": [100_000] * 6,
        }
    ).set_index("trade_date", drop=False)

    features = compute_underlying_features(
        df,
        breadth_advance_decline_ratio=2.0,
        breadth_new_high_ratio=0.36,
    )

    assert features["breadth_advance_decline_ratio"] == 2.0
    assert features["breadth_new_high_ratio"] == 0.36
