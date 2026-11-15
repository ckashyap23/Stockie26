from __future__ import annotations


from dataclasses import dataclass
from typing import Literal

RawSignal = Literal["CALL", "PUT", "NO_POSITION"]
Direction = Literal["BULLISH", "BEARISH", "NEUTRAL"]
Regime = Literal["TREND_UP", "TREND_DOWN", "RANGE", "CHOPPY", "UNKNOWN"]
Confidence = Literal["LOW", "MEDIUM", "HIGH"]
SetupType = Literal[
    "TREND_UP_PULLBACK_LONG",
    "TREND_UP_BREAKOUT_LONG",
    "TREND_DOWN_RALLY_SHORT",
    "TREND_DOWN_BREAKDOWN_SHORT",
    "RANGE_LOWER_BAND_LONG",
    "RANGE_UPPER_BAND_SHORT",
    "NO_SETUP",
]
OptionBias = Literal[
    "BULLISH_STRONG",
    "BULLISH_MODERATE",
    "BEARISH_STRONG",
    "BEARISH_MODERATE",
    "NEUTRAL",
]


@dataclass(frozen=True)
class UnderlyingFeatureSnapshot:
    symbol: str
    trade_date: str

    close: float | None
    volume: float | None

    ma10: float | None
    ma20: float | None
    ma50: float | None
    ma90: float | None

    ma20_slope: float | None
    ma50_slope: float | None

    rsi14: float | None
    atr14: float | None

    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    bb_width: float | None

    ret_5d: float | None
    ret_20d: float | None
    ret_60d: float | None
    volatility_20d: float | None

    volume_avg_20d: float | None
    volume_ratio: float | None

    trend_efficiency: float | None
    range_position: float | None

    relative_strength_vs_sector: float | None
    relative_strength_vs_benchmark: float | None

    distance_from_52w_high_pct: float | None = None
    distance_from_52w_low_pct: float | None = None


@dataclass(frozen=True)
class RegimeSnapshot:
    stock_regime: Regime
    sector_regime: Regime | None
    benchmark_regime: Regime | None

    stock_regime_confidence: float | None
    sector_regime_confidence: float | None
    benchmark_regime_confidence: float | None

    regime_reasons: list[str]


@dataclass(frozen=True)
class StrategySignal:
    strategy_name: str
    raw_signal: RawSignal
    direction: Direction
    setup_type: SetupType

    score: float
    confidence: Confidence

    expected_holding_days: int
    expected_move_pct: float | None
    expected_move_abs: float | None

    stop_loss_pct: float | None
    target_pct: float | None
    reward_risk: float | None

    reasons: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class UnderlyingView:
    symbol: str
    trade_date: str

    raw_signal: RawSignal
    direction: Direction

    stock_regime: Regime
    sector_regime: Regime | None
    benchmark_regime: Regime | None

    primary_strategy: str | None
    setup_type: SetupType

    strength_score: float
    confidence: Confidence

    expected_move_pct: float | None
    expected_move_abs: float | None
    expected_holding_days: int

    atr14: float | None
    volatility_20d: float | None
    volume_ratio: float | None
    relative_strength_vs_sector: float | None
    relative_strength_vs_benchmark: float | None

    stock_technical_score: float
    sector_confirmation_score: float
    benchmark_confirmation_score: float
    relative_strength_score: float
    volume_confirmation_score: float
    risk_quality_score: float
    regime_quality_score: float

    strategy_signals: list[StrategySignal]

    reasons: list[str]
    warnings: list[str]

    is_option_eligible: bool
    option_bias: OptionBias
