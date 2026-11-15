from __future__ import annotations


from dataclasses import replace

from .schema import OptionStrategyCandidate


def calculate_strategy_risk(candidate: OptionStrategyCandidate) -> OptionStrategyCandidate:
    total_delta = _sum_greek(candidate, "delta")
    total_gamma = _sum_greek(candidate, "gamma")
    total_theta = _sum_greek(candidate, "theta")
    total_vega = _sum_greek(candidate, "vega")
    warnings = list(candidate.warnings)
    if candidate.max_loss is None or candidate.max_loss <= 0:
        warnings.append("max loss missing or invalid")
    if candidate.strategy_type in {"BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"} and (
        candidate.reward_risk is None or candidate.reward_risk < 1.0
    ):
        warnings.append("spread reward/risk below threshold")
    if candidate.direction == "BULLISH" and total_delta is not None and total_delta <= 0:
        warnings.append("bullish strategy has non-positive total delta")
    if candidate.direction == "BEARISH" and total_delta is not None and total_delta >= 0:
        warnings.append("bearish strategy has non-negative total delta")
    return replace(
        candidate,
        total_delta=total_delta,
        total_gamma=total_gamma,
        total_theta=total_theta,
        total_vega=total_vega,
        warnings=warnings,
    )


def _sum_greek(candidate: OptionStrategyCandidate, greek: str) -> float | None:
    values: list[float] = []
    for leg in candidate.legs:
        value = getattr(leg.contract, greek)
        if value is None:
            continue
        sign = 1.0 if leg.side == "BUY" else -1.0
        values.append(sign * float(value) * leg.quantity)
    if not values:
        return None
    return sum(values)
