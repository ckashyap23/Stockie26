from __future__ import annotations


from dataclasses import dataclass
from typing import Literal

OptionType = Literal["CE", "PE"]
OptionSide = Literal["BUY", "SELL"]
OptionStrategyType = Literal[
    "LONG_CALL",
    "LONG_PUT",
    "BULL_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
    "NO_TRADE",
]
OptionBias = Literal[
    "BULLISH_STRONG",
    "BULLISH_MODERATE",
    "BEARISH_STRONG",
    "BEARISH_MODERATE",
    "NEUTRAL",
]
SelectionConfidence = Literal["LOW", "MEDIUM", "HIGH"]


@dataclass(frozen=True)
class OptionContract:
    instrument_token: int | None
    tradingsymbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: OptionType

    last_price: float
    bid: float | None
    ask: float | None
    volume: int | None
    open_interest: int | None

    snapshot_time: str | None = None
    calc_time: str | None = None

    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


@dataclass(frozen=True)
class OptionFeatures:
    tradingsymbol: str
    expiry: str
    strike: float
    option_type: OptionType

    days_to_expiry: int
    moneyness_pct: float | None
    distance_from_spot_pct: float | None

    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None

    spread_pct: float | None
    mid_price: float | None
    liquidity_score: float
    theta_burn_pct_per_day: float | None

    iv_rank_90d: float | None
    iv_percentile_90d: float | None
    iv_vs_atm_pct: float | None
    iv_vs_neighbor_median_pct: float | None

    is_iv_outlier: bool
    is_liquid: bool
    is_tradeable: bool
    rejection_reasons: list[str]


@dataclass(frozen=True)
class OptionLeg:
    side: OptionSide
    contract: OptionContract
    features: OptionFeatures
    quantity: int = 1


@dataclass(frozen=True)
class OptionStrategyCandidate:
    strategy_type: OptionStrategyType
    legs: list[OptionLeg]

    direction: str
    expected_underlying_move_pct: float | None
    expected_underlying_move_abs: float | None
    expected_holding_days: int

    entry_debit_or_credit: float | None
    max_profit: float | None
    max_loss: float | None
    breakeven: float | None
    reward_risk: float | None

    total_delta: float | None
    total_gamma: float | None
    total_theta: float | None
    total_vega: float | None

    score: float
    confidence: SelectionConfidence
    reasons: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class OptionSelectionResult:
    underlying: str
    trade_date: str
    selected_strategy: OptionStrategyCandidate
    option_bias: OptionBias
    no_trade_reason: str | None
    evaluated_candidate_count: int
