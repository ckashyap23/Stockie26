from __future__ import annotations


import pandas as pd

from .features import PredictionInput, compute_underlying_features, get_closes

TREND_RETURN_THRESHOLD = 0.02
TREND_EFFICIENCY_THRESHOLD = 0.25
RANGE_RETURN_THRESHOLD = 0.012
FLAT_SLOPE_THRESHOLD = 0.01
HIGH_VOLATILITY_THRESHOLD = 0.025
MODERATE_VOLATILITY_MAX = 0.03
LOW_TREND_EFFICIENCY_THRESHOLD = 0.25


def detect_regime(window: PredictionInput) -> str:
    features = compute_underlying_features(window)
    closes = get_closes(window)
    if len(closes) < 10:            # was 60 — only block if truly insufficient data
        return "UNKNOWN"

    close = float(closes.iloc[-1])
    ma20 = _feature_float(features.get("ma20"))
    ma50 = _feature_float(features.get("ma50"))
    ma20_slope = _feature_float(features.get("ma20_slope"))
    ma50_slope = _feature_float(features.get("ma50_slope"))
    ret_60d = _feature_float(features.get("ret_60d"))
    trend_efficiency = _feature_float(features.get("trend_efficiency_60d"))
    volatility_20d = _feature_float(features.get("volatility_20d"))
    range_position = _feature_float(features.get("range_position_20d"))

    if None not in (ma20, ma50, ma20_slope, ma50_slope, ret_60d, trend_efficiency):
        if (
            close > ma20 > ma50
            and ma20_slope > 0
            and ma50_slope > 0
            and ret_60d > TREND_RETURN_THRESHOLD
            and trend_efficiency > TREND_EFFICIENCY_THRESHOLD
        ):
            return "TREND_UP"
        if (
            close < ma20 < ma50
            and ma20_slope < 0
            and ma50_slope < 0
            and ret_60d < -TREND_RETURN_THRESHOLD
            and trend_efficiency > TREND_EFFICIENCY_THRESHOLD
        ):
            return "TREND_DOWN"

    ret_is_small = ret_60d is not None and abs(ret_60d) <= RANGE_RETURN_THRESHOLD
    slopes_flat = (
        ma20_slope is not None
        and ma50_slope is not None
        and abs(ma20_slope) <= FLAT_SLOPE_THRESHOLD
        and abs(ma50_slope) <= FLAT_SLOPE_THRESHOLD
    )
    oscillating_in_range = range_position is not None and 0.15 <= range_position <= 0.85
    volatility_moderate = volatility_20d is not None and volatility_20d <= MODERATE_VOLATILITY_MAX
    trend_efficiency_low = trend_efficiency is not None and trend_efficiency <= LOW_TREND_EFFICIENCY_THRESHOLD
    volatility_high = volatility_20d is not None and volatility_20d >= HIGH_VOLATILITY_THRESHOLD

    if ret_is_small and trend_efficiency_low and volatility_high:
        return "CHOPPY"
    if ret_is_small and slopes_flat and oscillating_in_range and volatility_moderate:
        return "RANGE"
    if ret_is_small:
        return "RANGE"
    if trend_efficiency_low and volatility_high:
        return "CHOPPY"
    return "RANGE"


def _feature_float(value: object) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
