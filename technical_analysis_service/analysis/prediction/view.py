from __future__ import annotations


from .aggregator import aggregate_strategy_signals
from .expected_move import estimate_expected_move
from .explanation import build_reasons
from .schema import (
    Confidence,
    Direction,
    OptionBias,
    RawSignal,
    RegimeSnapshot,
    StrategySignal,
    UnderlyingFeatureSnapshot,
    UnderlyingView,
)
from .scoring import confidence_from_score, score_underlying_view


def build_underlying_view(
    symbol: str,
    trade_date: str,
    stock_features: UnderlyingFeatureSnapshot,
    regime_snapshot: RegimeSnapshot,
    strategy_signals: list[StrategySignal],
    ruleset_version: str = "v1",
) -> UnderlyingView:
    _ = ruleset_version
    direction, primary_strategy_signal, has_conflict = aggregate_strategy_signals(strategy_signals)
    setup_type = primary_strategy_signal.setup_type if primary_strategy_signal else "NO_SETUP"
    if has_conflict:
        direction = "NEUTRAL"
        setup_type = "NO_SETUP"

    score_breakdown = score_underlying_view(
        stock_features,
        regime_snapshot,
        direction,
        setup_type,
        reward_risk=primary_strategy_signal.reward_risk if primary_strategy_signal else None,
    )
    strength_score = score_breakdown.final_score
    raw_signal = _raw_signal_from_score(direction, strength_score)
    if raw_signal == "NO_POSITION":
        direction = "NEUTRAL"

    confidence = confidence_from_score(strength_score)
    expected_move_pct, expected_move_abs, expected_holding_days = estimate_expected_move(
        stock_features,
        regime_snapshot.stock_regime,
        setup_type,
    )
    warnings = _dedupe(
        score_breakdown.warnings
        + [warning for signal in strategy_signals for warning in signal.warnings]
        + (["Strategy signals are conflicting"] if has_conflict else [])
    )
    reasons = build_reasons(
        stock_features,
        regime_snapshot,
        direction,
        setup_type,
        primary_strategy_signal.strategy_name if primary_strategy_signal else None,
        strategy_signals,
        score_breakdown,
    )
    option_bias = derive_option_bias(raw_signal, direction, strength_score)
    is_option_eligible = _is_option_eligible(
        raw_signal=raw_signal,
        strength_score=strength_score,
        confidence=confidence,
        expected_move_pct=expected_move_pct,
        stock_regime=regime_snapshot.stock_regime,
        setup_type=setup_type,
        has_conflict=has_conflict,
    )
    if not is_option_eligible:
        option_bias = "NEUTRAL"

    return UnderlyingView(
        symbol=symbol.upper(),
        trade_date=trade_date,
        raw_signal=raw_signal,
        direction=direction,
        stock_regime=regime_snapshot.stock_regime,
        sector_regime=regime_snapshot.sector_regime,
        benchmark_regime=regime_snapshot.benchmark_regime,
        primary_strategy=primary_strategy_signal.strategy_name if primary_strategy_signal else None,
        setup_type=setup_type,
        strength_score=strength_score,
        confidence=confidence,
        expected_move_pct=expected_move_pct,
        expected_move_abs=expected_move_abs,
        expected_holding_days=expected_holding_days,
        atr14=stock_features.atr14,
        volatility_20d=stock_features.volatility_20d,
        volume_ratio=stock_features.volume_ratio,
        relative_strength_vs_sector=stock_features.relative_strength_vs_sector,
        relative_strength_vs_benchmark=stock_features.relative_strength_vs_benchmark,
        stock_technical_score=score_breakdown.stock_technical_score,
        sector_confirmation_score=score_breakdown.sector_confirmation_score,
        benchmark_confirmation_score=score_breakdown.benchmark_confirmation_score,
        relative_strength_score=score_breakdown.relative_strength_score,
        volume_confirmation_score=score_breakdown.volume_confirmation_score,
        risk_quality_score=score_breakdown.risk_quality_score,
        regime_quality_score=score_breakdown.regime_quality_score,
        strategy_signals=strategy_signals,
        reasons=reasons,
        warnings=warnings,
        is_option_eligible=is_option_eligible,
        option_bias=option_bias,
    )


def derive_option_bias(raw_signal: RawSignal, direction: Direction, strength_score: float) -> OptionBias:
    if raw_signal == "NO_POSITION" or direction == "NEUTRAL" or strength_score < 65:
        return "NEUTRAL"
    if direction == "BULLISH":
        return "BULLISH_STRONG" if strength_score >= 80 else "BULLISH_MODERATE"
    if direction == "BEARISH":
        return "BEARISH_STRONG" if strength_score >= 80 else "BEARISH_MODERATE"
    return "NEUTRAL"


def _raw_signal_from_score(direction: Direction, score: float) -> RawSignal:
    # raw_signal follows the aggregator direction; strength_score is informational quality.
    # is_option_eligible() (score >= 65) is the gate for actual option trades.
    _ = score
    if direction == "BULLISH":
        return "CALL"
    if direction == "BEARISH":
        return "PUT"
    return "NO_POSITION"


def _is_option_eligible(
    raw_signal: RawSignal,
    strength_score: float,
    confidence: Confidence,
    expected_move_pct: float | None,
    stock_regime: str,
    setup_type: str,
    has_conflict: bool,
) -> bool:
    return (
        raw_signal in {"CALL", "PUT"}
        and strength_score >= 65
        and confidence in {"MEDIUM", "HIGH"}
        and expected_move_pct is not None
        and expected_move_pct > 0
        and stock_regime != "CHOPPY"
        and setup_type != "NO_SETUP"
        and not has_conflict
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output
