from __future__ import annotations


from .features import (
    FEATURE_COLUMNS,
    FeatureOutput,
    PredictionInput,
    compute_atr,
    compute_average_volume,
    compute_relative_strength_vs_sector,
    compute_return,
    compute_rsi,
    compute_trend_efficiency,
    compute_true_range,
    compute_underlying_features,
    get_closes,
    get_column,
)
from .regime import detect_regime
from .schema import (
    Confidence,
    Direction,
    OptionBias,
    RawSignal,
    Regime,
    RegimeSnapshot,
    SetupType,
    StrategySignal,
    UnderlyingFeatureSnapshot,
    UnderlyingView,
)
from .snapshot import build_feature_snapshot, build_regime_snapshot
from .aggregator import build_strategy_signal, build_strategy_signals
from .view import build_underlying_view
from .strategies import (
    BUILTIN_UNDERLYING_STRATEGIES,
    SignalValue,
    UnderlyingPredictionFunction,
    UnderlyingStrategyDefinition,
    no_position,
    range_gated_signal,
    signal_bollinger_mean_reversion,
    signal_ma_trend,
    signal_range_breakout,
    signal_rsi_mean_reversion,
    trend_gated_range_breakout,
)

DEFAULT_LOOKBACK_DAYS = 90

__all__ = [
    "BUILTIN_UNDERLYING_STRATEGIES",
    "DEFAULT_LOOKBACK_DAYS",
    "FEATURE_COLUMNS",
    "FeatureOutput",
    "Confidence",
    "Direction",
    "PredictionInput",
    "OptionBias",
    "RawSignal",
    "Regime",
    "RegimeSnapshot",
    "SetupType",
    "StrategySignal",
    "UnderlyingFeatureSnapshot",
    "UnderlyingView",
    "SignalValue",
    "UnderlyingPredictionFunction",
    "UnderlyingStrategyDefinition",
    "compute_atr",
    "compute_average_volume",
    "compute_relative_strength_vs_sector",
    "compute_return",
    "compute_rsi",
    "compute_trend_efficiency",
    "compute_true_range",
    "compute_underlying_features",
    "detect_regime",
    "get_closes",
    "get_column",
    "build_feature_snapshot",
    "build_regime_snapshot",
    "build_strategy_signal",
    "build_strategy_signals",
    "build_underlying_view",
    "no_position",
    "range_gated_signal",
    "signal_bollinger_mean_reversion",
    "signal_ma_trend",
    "signal_range_breakout",
    "signal_rsi_mean_reversion",
    "trend_gated_range_breakout",
]
