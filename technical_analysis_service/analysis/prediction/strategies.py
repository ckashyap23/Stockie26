from __future__ import annotations


from dataclasses import dataclass
from functools import partial
from typing import Callable

import pandas as pd

from .features import PredictionInput, compute_rsi, compute_underlying_features, get_closes, get_column
from .regime import detect_regime

SignalValue = str
UnderlyingPredictionFunction = Callable[[PredictionInput], SignalValue]


def signal_rsi_mean_reversion(
    window: PredictionInput,
    rsi_period: int = 14,
    overbought: float = 70.0,
    oversold: float = 30.0,
) -> SignalValue:
    closes = get_closes(window)
    if len(closes) < rsi_period + 2:
        return "NO_POSITION"
    current_rsi = float(compute_rsi(closes, period=rsi_period).iloc[-1])
    if current_rsi >= overbought:
        return "PUT"
    if current_rsi <= oversold:
        return "CALL"
    return "NO_POSITION"


def signal_ma_trend(
    window: PredictionInput,
    short_window: int = 10,
    long_window: int = 20,
    band: float = 0.001,
) -> SignalValue:
    closes = get_closes(window)
    if len(closes) < long_window:
        return "NO_POSITION"
    short_ma = float(closes.iloc[-short_window:].mean())
    long_ma = float(closes.iloc[-long_window:].mean())
    if long_ma == 0:
        return "NO_POSITION"
    spread_pct = (short_ma - long_ma) / long_ma
    if spread_pct > band:
        return "CALL"
    if spread_pct < -band:
        return "PUT"
    return "NO_POSITION"


def signal_bollinger_mean_reversion(
    window: PredictionInput,
    bb_period: int = 20,
    num_std: float = 2.0,
) -> SignalValue:
    closes = get_closes(window)
    if bb_period == 20 and num_std == 2.0:
        features = compute_underlying_features(window)
        upper = features.get("bb_upper")
        lower = features.get("bb_lower")
    elif len(closes) >= bb_period:
        middle = closes.rolling(bb_period).mean().iloc[-1]
        std = closes.rolling(bb_period).std(ddof=0).iloc[-1]
        upper = middle + num_std * std
        lower = middle - num_std * std
    else:
        upper = None
        lower = None
    if upper is None or lower is None or closes.empty or pd.isna(upper) or pd.isna(lower):
        return "NO_POSITION"
    last = float(closes.iloc[-1])
    if last > float(upper):
        return "PUT"
    if last < float(lower):
        return "CALL"
    return "NO_POSITION"


def signal_range_breakout(window: PredictionInput, lookback: int = 20) -> SignalValue:
    closes = get_closes(window)
    highs = get_column(window, "high_price")
    lows = get_column(window, "low_price")
    if highs is None or lows is None or len(closes) < 2:
        return "NO_POSITION"
    last_close = float(closes.iloc[-1])
    recent_high = float(highs.iloc[:-1].tail(lookback).max())
    recent_low = float(lows.iloc[:-1].tail(lookback).min())
    if last_close > recent_high:
        return "CALL"
    if last_close < recent_low:
        return "PUT"
    return "NO_POSITION"


def no_position(window: PredictionInput) -> SignalValue:
    _ = window
    return "NO_POSITION"


def range_gated_signal(window: PredictionInput, signal_fn: UnderlyingPredictionFunction) -> SignalValue:
    if detect_regime(window) != "RANGE":
        return "NO_POSITION"
    return signal_fn(window)


def trend_gated_range_breakout(window: PredictionInput, regime: str) -> SignalValue:
    if detect_regime(window) != regime:
        return "NO_POSITION"
    return signal_range_breakout(window)


@dataclass(frozen=True)
class UnderlyingStrategyDefinition:
    name: str
    predict: UnderlyingPredictionFunction
    description: str = ""


BUILTIN_UNDERLYING_STRATEGIES: dict[str, UnderlyingStrategyDefinition] = {
    "BollingerMeanReversion": UnderlyingStrategyDefinition(
        name="BollingerMeanReversion",
        predict=signal_bollinger_mean_reversion,
        description="Bollinger-band mean reversion.",
    ),
    "MaTrend_001": UnderlyingStrategyDefinition(
        name="MaTrend_001",
        predict=partial(signal_ma_trend, short_window=10, long_window=20, band=0.001),
        description="MA trend using 10/20 day moving averages and a 0.1% band.",
    ),
    "RsiMeanReversion_6040": UnderlyingStrategyDefinition(
        name="RsiMeanReversion_6040",
        predict=partial(signal_rsi_mean_reversion, rsi_period=14, overbought=60.0, oversold=40.0),
        description="RSI14 mean reversion using 60/40 thresholds.",
    ),
    "rangeBollingerMeanReversion": UnderlyingStrategyDefinition(
        name="rangeBollingerMeanReversion",
        predict=partial(range_gated_signal, signal_fn=signal_bollinger_mean_reversion),
        description="Bollinger mean reversion enabled only in RANGE regime.",
    ),
    "rangeRsiMeanReversion_6040": UnderlyingStrategyDefinition(
        name="rangeRsiMeanReversion_6040",
        predict=partial(
            range_gated_signal,
            signal_fn=partial(signal_rsi_mean_reversion, rsi_period=14, overbought=60.0, oversold=40.0),
        ),
        description="RSI14 mean reversion using 60/40 thresholds enabled only in RANGE regime.",
    ),
    "trendDownRangeBreakout": UnderlyingStrategyDefinition(
        name="trendDownRangeBreakout",
        predict=partial(trend_gated_range_breakout, regime="TREND_DOWN"),
        description="Range breakdown enabled only in TREND_DOWN regime.",
    ),
    "trendUpRangeBreakout": UnderlyingStrategyDefinition(
        name="trendUpRangeBreakout",
        predict=partial(trend_gated_range_breakout, regime="TREND_UP"),
        description="Range breakout enabled only in TREND_UP regime.",
    ),
    "choppy": UnderlyingStrategyDefinition(
        name="choppy",
        predict=no_position,
        description="Baseline CHOPPY regime no-trade strategy.",
    ),
    "unknown": UnderlyingStrategyDefinition(
        name="unknown",
        predict=no_position,
        description="Baseline UNKNOWN regime no-trade strategy.",
    ),
}
