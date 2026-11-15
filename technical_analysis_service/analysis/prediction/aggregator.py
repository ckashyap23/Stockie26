from __future__ import annotations


from .expected_move import estimate_expected_move
from .schema import Direction, RawSignal, RegimeSnapshot, SetupType, StrategySignal, UnderlyingFeatureSnapshot
from .scoring import confidence_from_score, score_underlying_view


def direction_from_raw_signal(raw_signal: str) -> Direction:
    if raw_signal == "CALL":
        return "BULLISH"
    if raw_signal == "PUT":
        return "BEARISH"
    return "NEUTRAL"


def setup_type_for_signal(
    strategy_name: str,
    raw_signal: RawSignal,
    regime: RegimeSnapshot,
    features: UnderlyingFeatureSnapshot,
) -> SetupType:
    if raw_signal == "NO_POSITION" or regime.stock_regime == "CHOPPY":
        return "NO_SETUP"
    name = strategy_name.lower()
    breakout = "breakout" in name or _is_breakout(raw_signal, features)
    if raw_signal == "CALL":
        if regime.stock_regime == "TREND_UP" and breakout:
            return "TREND_UP_BREAKOUT_LONG"
        if regime.stock_regime == "TREND_UP":
            return "TREND_UP_PULLBACK_LONG"
        if regime.stock_regime == "RANGE":
            return "RANGE_LOWER_BAND_LONG"
    if raw_signal == "PUT":
        if regime.stock_regime == "TREND_DOWN" and breakout:
            return "TREND_DOWN_BREAKDOWN_SHORT"
        if regime.stock_regime == "TREND_DOWN":
            return "TREND_DOWN_RALLY_SHORT"
        if regime.stock_regime == "RANGE":
            return "RANGE_UPPER_BAND_SHORT"
    return "NO_SETUP"


def build_strategy_signal(
    strategy_name: str,
    raw_signal_value: str,
    features: UnderlyingFeatureSnapshot,
    regime: RegimeSnapshot,
) -> StrategySignal:
    raw_signal: RawSignal = raw_signal_value if raw_signal_value in {"CALL", "PUT"} else "NO_POSITION"  # type: ignore[assignment]
    direction = direction_from_raw_signal(raw_signal)
    setup_type = setup_type_for_signal(strategy_name, raw_signal, regime, features)
    expected_move_pct, expected_move_abs, holding_days = estimate_expected_move(
        features,
        regime.stock_regime,
        setup_type,
    )
    score = 0.0
    reasons: list[str] = []
    warnings: list[str] = []
    if raw_signal != "NO_POSITION":
        breakdown = score_underlying_view(features, regime, direction, setup_type)
        score = breakdown.final_score
        warnings.extend(breakdown.warnings)
        reasons.append(f"{strategy_name} produced {raw_signal}")
    else:
        reasons.append(f"{strategy_name} produced NO_POSITION")
    return StrategySignal(
        strategy_name=strategy_name,
        raw_signal=raw_signal,
        direction=direction,
        setup_type=setup_type,
        score=score,
        confidence=confidence_from_score(score),
        expected_holding_days=holding_days,
        expected_move_pct=expected_move_pct,
        expected_move_abs=expected_move_abs,
        stop_loss_pct=None,
        target_pct=expected_move_pct,
        reward_risk=None,
        reasons=reasons,
        warnings=warnings,
    )


def build_strategy_signals(
    predictions: dict[str, str],
    features: UnderlyingFeatureSnapshot,
    regime: RegimeSnapshot,
) -> list[StrategySignal]:
    return [
        build_strategy_signal(name, raw_signal, features, regime)
        for name, raw_signal in sorted(predictions.items())
    ]


def aggregate_strategy_signals(strategy_signals: list[StrategySignal]) -> tuple[Direction, StrategySignal | None, bool]:
    eligible = [signal for signal in strategy_signals if signal.score >= 30 and signal.raw_signal in {"CALL", "PUT"}]
    bullish_score = sum(signal.score for signal in eligible if signal.raw_signal == "CALL")
    bearish_score = sum(signal.score for signal in eligible if signal.raw_signal == "PUT")
    conflict = bullish_score >= 40 and bearish_score >= 40
    if bullish_score >= bearish_score * 1.25 and bullish_score >= 30:
        direction: Direction = "BULLISH"
    elif bearish_score >= bullish_score * 1.25 and bearish_score >= 30:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
    aligned = [signal for signal in eligible if signal.direction == direction]
    primary = max(aligned, key=lambda signal: signal.score, default=None)
    return direction, primary, conflict


def _is_breakout(raw_signal: RawSignal, features: UnderlyingFeatureSnapshot) -> bool:
    if raw_signal == "CALL" and features.close is not None and features.bb_upper is not None:
        return features.close >= features.bb_upper
    if raw_signal == "PUT" and features.close is not None and features.bb_lower is not None:
        return features.close <= features.bb_lower
    return False
