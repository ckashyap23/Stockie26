from __future__ import annotations

from .option_features import compute_option_features_for_chain
from .repository import load_option_chain_with_calcs
from .risk import calculate_strategy_risk
from .schema import OptionBias, OptionSelectionResult, OptionStrategyCandidate
from .scoring import score_option_candidate
from .strategy_builder import build_strategy_candidates
from .strategy_rules import choose_option_strategy_type
from src.technical_analysis.prediction.schema import UnderlyingView

MIN_UNDERLYING_SCORE = 65
MIN_CANDIDATE_SCORE = 65


def select_option_strategy(
    db_client,
    underlying_view: UnderlyingView,
    spot_price: float,
    as_of_time: str | None = None,
    atm_iv_history_90d: list[float] | None = None,
) -> OptionSelectionResult:
    prediction_side = _prediction_side(underlying_view)
    if prediction_side == "NO_POSITION":
        return no_trade_result(underlying_view.symbol, underlying_view.trade_date, "Underlying signal is NO_POSITION")

    option_bias = _option_bias_from_prediction(prediction_side, underlying_view.strength_score)

    contracts = load_option_chain_with_calcs(db_client, underlying_view.symbol, as_of_time=as_of_time)
    if not contracts:
        return no_trade_result(underlying_view.symbol, underlying_view.trade_date, "No option chain rows available", option_bias)

    features = compute_option_features_for_chain(contracts, spot_price, underlying_view.trade_date, atm_iv_history_90d)
    atm_iv_rank = _atm_metric(features, spot_price, metric="iv_rank_90d")
    atm_iv_percentile = _atm_metric(features, spot_price, metric="iv_percentile_90d")
    min_dte = min((feature.days_to_expiry for feature in features.values()), default=None)
    strategy_type = choose_option_strategy_type(
        option_bias,
        atm_iv_rank,
        atm_iv_percentile,
        underlying_view.expected_move_pct,
        underlying_view.expected_holding_days,
        min_dte,
    )
    if strategy_type == "NO_TRADE":
        return no_trade_result(underlying_view.symbol, underlying_view.trade_date, "Strategy rules returned NO_TRADE", option_bias)

    candidates = build_strategy_candidates(strategy_type, contracts, features, underlying_view, spot_price)
    if not candidates:
        return no_trade_result(underlying_view.symbol, underlying_view.trade_date, "No candidates passed base filters", option_bias)

    scored = [
        score_option_candidate(calculate_strategy_risk(candidate), underlying_view, features)
        for candidate in candidates
    ]
    scored.sort(key=lambda candidate: candidate.score, reverse=True)
    best = scored[0]
    return OptionSelectionResult(
        underlying=underlying_view.symbol,
        trade_date=underlying_view.trade_date,
        selected_strategy=best,
        option_bias=option_bias,
        no_trade_reason=None,
        evaluated_candidate_count=len(scored),
    )


def no_trade_result(
    underlying: str,
    trade_date: str,
    reason: str,
    option_bias: OptionBias = "NEUTRAL",
    evaluated_candidate_count: int = 0,
) -> OptionSelectionResult:
    return OptionSelectionResult(
        underlying=underlying,
        trade_date=trade_date,
        selected_strategy=OptionStrategyCandidate(
            strategy_type="NO_TRADE",
            legs=[],
            direction="NEUTRAL",
            expected_underlying_move_pct=None,
            expected_underlying_move_abs=None,
            expected_holding_days=0,
            entry_debit_or_credit=None,
            max_profit=None,
            max_loss=None,
            breakeven=None,
            reward_risk=None,
            total_delta=None,
            total_gamma=None,
            total_theta=None,
            total_vega=None,
            score=0.0,
            confidence="LOW",
            reasons=[],
            warnings=[reason],
        ),
        option_bias=option_bias,
        no_trade_reason=reason,
        evaluated_candidate_count=evaluated_candidate_count,
    )


def _atm_metric(features: dict[str, object], spot_price: float, metric: str) -> float | None:
    option_features = list(features.values())
    if not option_features:
        return None
    atm = min(option_features, key=lambda feature: abs(feature.strike - spot_price))  # type: ignore[attr-defined]
    value = getattr(atm, metric, None)
    return float(value) if value is not None else None


def _prediction_side(underlying_view: UnderlyingView) -> str:
    direction = str(underlying_view.direction)
    if direction in {"CALL", "PUT", "NO_POSITION"}:
        return direction
    raw_signal = str(underlying_view.raw_signal)
    if raw_signal in {"CALL", "PUT"}:
        return raw_signal
    if direction == "BULLISH":
        return "CALL"
    if direction == "BEARISH":
        return "PUT"
    return "NO_POSITION"


def _option_bias_from_prediction(prediction_side: str, strength_score: float) -> OptionBias:
    if prediction_side == "CALL":
        return "BULLISH_STRONG" if strength_score >= 80 else "BULLISH_MODERATE"
    if prediction_side == "PUT":
        return "BEARISH_STRONG" if strength_score >= 80 else "BEARISH_MODERATE"
    return "NEUTRAL"
