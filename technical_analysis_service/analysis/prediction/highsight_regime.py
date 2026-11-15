from __future__ import annotations


from typing import Any

import pandas as pd

HINDSIGHT_LOOKAHEAD_DAYS = 10
HINDSIGHT_MIN_LOOKAHEAD_DAYS = 5
HINDSIGHT_TREND_RETURN_THRESHOLD = 0.02
HINDSIGHT_TREND_EFFICIENCY_THRESHOLD = 0.35
HINDSIGHT_RANGE_RETURN_THRESHOLD = 0.01
HINDSIGHT_CHOPPY_EFFICIENCY_MAX = 0.25
HINDSIGHT_CHOPPY_RANGE_THRESHOLD = 0.03


def compute_hindsight_regime(
    ohlcv_df: pd.DataFrame,
    current_idx: int | None,
    lookahead_days: int = HINDSIGHT_LOOKAHEAD_DAYS,
    min_lookahead_days: int = HINDSIGHT_MIN_LOOKAHEAD_DAYS,
) -> dict[str, Any]:
    if current_idx is None or current_idx < 0 or current_idx >= len(ohlcv_df):
        return _unknown_result()

    window = ohlcv_df.iloc[current_idx: current_idx + lookahead_days + 1]
    realized_days = len(window) - 1
    if realized_days < min_lookahead_days:
        result = _unknown_result()
        result["hindsight_regime_lookahead_days"] = realized_days
        return result

    closes = window["close_price"].astype(float)
    current_close = float(closes.iloc[0])
    future_close = float(closes.iloc[-1])
    if current_close == 0:
        return _unknown_result()

    forward_return = future_close / current_close - 1.0
    path_move = float(closes.diff().abs().sum())
    forward_efficiency = abs(future_close - current_close) / path_move if path_move else 0.0
    realized_range = _realized_range_pct(window, current_close)

    hindsight_regime = _classify_hindsight_regime(
        forward_return=forward_return,
        forward_efficiency=forward_efficiency,
        realized_range=realized_range,
    )
    return {
        "hindsight_regime": hindsight_regime,
        "hindsight_regime_lookahead_days": realized_days,
        "hindsight_regime_forward_return": round(forward_return, 6),
        "hindsight_regime_forward_efficiency": round(forward_efficiency, 6),
        "hindsight_regime_realized_range": round(realized_range, 6),
    }


def _classify_hindsight_regime(
    forward_return: float,
    forward_efficiency: float,
    realized_range: float,
) -> str:
    if (
        forward_return >= HINDSIGHT_TREND_RETURN_THRESHOLD
        and forward_efficiency >= HINDSIGHT_TREND_EFFICIENCY_THRESHOLD
    ):
        return "TREND_UP"
    if (
        forward_return <= -HINDSIGHT_TREND_RETURN_THRESHOLD
        and forward_efficiency >= HINDSIGHT_TREND_EFFICIENCY_THRESHOLD
    ):
        return "TREND_DOWN"
    if (
        abs(forward_return) <= HINDSIGHT_RANGE_RETURN_THRESHOLD
        and forward_efficiency <= HINDSIGHT_CHOPPY_EFFICIENCY_MAX
        and realized_range >= HINDSIGHT_CHOPPY_RANGE_THRESHOLD
    ):
        return "CHOPPY"
    return "RANGE"


def _realized_range_pct(window: pd.DataFrame, current_close: float) -> float:
    if current_close == 0:
        return 0.0
    highs = window["high_price"].astype(float) if "high_price" in window.columns else window["close_price"].astype(float)
    lows = window["low_price"].astype(float) if "low_price" in window.columns else window["close_price"].astype(float)
    return (float(highs.max()) - float(lows.min())) / current_close


def _unknown_result() -> dict[str, Any]:
    return {
        "hindsight_regime": "UNKNOWN",
        "hindsight_regime_lookahead_days": None,
        "hindsight_regime_forward_return": None,
        "hindsight_regime_forward_efficiency": None,
        "hindsight_regime_realized_range": None,
    }