from __future__ import annotations


from .schema import Direction, RegimeSnapshot, SetupType, StrategySignal, UnderlyingFeatureSnapshot
from .scoring import ScoreBreakdown


def build_reasons(
    features: UnderlyingFeatureSnapshot,
    regime: RegimeSnapshot,
    direction: Direction,
    setup_type: SetupType,
    primary_strategy: str | None,
    strategy_signals: list[StrategySignal],
    score: ScoreBreakdown,
) -> list[str]:
    reasons: list[str] = []
    if direction == "BULLISH":
        reasons.append(f"Stock is in {regime.stock_regime} regime for a bullish setup")
        if features.close is not None and features.ma20 is not None and features.close > features.ma20:
            reasons.append("Close is above MA20")
    elif direction == "BEARISH":
        reasons.append(f"Stock is in {regime.stock_regime} regime for a bearish setup")
        if features.close is not None and features.ma20 is not None and features.close < features.ma20:
            reasons.append("Close is below MA20")
    else:
        reasons.append("No directional setup survived aggregation")

    if primary_strategy:
        reasons.append(f"Primary strategy is {primary_strategy}")
    if setup_type != "NO_SETUP":
        reasons.append(f"Setup type is {setup_type}")
    if score.sector_confirmation_score > 0:
        reasons.append("Sector regime supports the signal")
    if score.benchmark_confirmation_score > 0:
        reasons.append("Benchmark regime supports or does not oppose the signal")
    if score.relative_strength_score > 0:
        reasons.append("Relative strength supports the direction")
    if score.volume_confirmation_score >= 7:
        reasons.append("Volume ratio confirms participation")

    for signal in strategy_signals:
        for reason in signal.reasons:
            if reason not in reasons:
                reasons.append(reason)
            if len(reasons) >= 10:
                return reasons
    return reasons[:10]
