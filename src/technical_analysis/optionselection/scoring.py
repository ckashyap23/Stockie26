from __future__ import annotations

from dataclasses import replace

from src.technical_analysis.prediction.schema import UnderlyingView

from .schema import OptionFeatures, OptionStrategyCandidate, SelectionConfidence


def score_option_candidate(
    candidate: OptionStrategyCandidate,
    underlying_view: UnderlyingView,
    features: dict[str, OptionFeatures],
) -> OptionStrategyCandidate:
    _ = features
    score = 0.0
    warnings = list(candidate.warnings)
    reasons = list(candidate.reasons)

    score += min(25.0, max(0.0, underlying_view.strength_score / 100.0 * 25.0))
    score += _directional_greek_score(candidate, warnings)
    score += _liquidity_score(candidate, warnings)
    score += _theta_score(candidate, warnings)
    score += _iv_score(candidate, warnings)
    score += _reward_risk_score(candidate, warnings)
    score += _expiry_fit_score(candidate, warnings)

    final_score = max(0.0, min(100.0, round(score, 4)))
    confidence = _confidence(final_score)
    if final_score >= 65:
        reasons.append(f"Candidate score {final_score} meets selection threshold")
    return replace(candidate, score=final_score, confidence=confidence, reasons=_dedupe(reasons), warnings=_dedupe(warnings))


def _directional_greek_score(candidate: OptionStrategyCandidate, warnings: list[str]) -> float:
    delta = candidate.total_delta
    if delta is None:
        warnings.append("missing aggregate delta")
        return 0.0
    if candidate.direction == "BULLISH":
        target = 0.30 if candidate.strategy_type == "LONG_CALL" else 0.15
        return 15.0 if delta >= target else 8.0 if delta > 0 else 0.0
    target = -0.30 if candidate.strategy_type == "LONG_PUT" else -0.15
    return 15.0 if delta <= target else 8.0 if delta < 0 else 0.0


def _liquidity_score(candidate: OptionStrategyCandidate, warnings: list[str]) -> float:
    if not candidate.legs:
        return 0.0
    avg = sum(leg.features.liquidity_score for leg in candidate.legs) / len(candidate.legs)
    score = avg / 100.0 * 15.0
    if any((leg.features.spread_pct or 0.0) > 0.05 for leg in candidate.legs if leg.side == "BUY"):
        warnings.append("buy leg spread above preferred limit")
        score -= 5.0
    return max(0.0, score)


def _theta_score(candidate: OptionStrategyCandidate, warnings: list[str]) -> float:
    burns = [leg.features.theta_burn_pct_per_day for leg in candidate.legs if leg.side == "BUY" and leg.features.theta_burn_pct_per_day is not None]
    if not burns:
        return 8.0
    worst = max(burns)
    if worst <= 0.05:
        return 15.0
    if worst <= 0.08:
        return 11.0
    if worst <= 0.12:
        warnings.append("theta burn above preferred limit")
        return 6.0
    warnings.append("theta burn above hard limit")
    return 0.0


def _iv_score(candidate: OptionStrategyCandidate, warnings: list[str]) -> float:
    buy_features = [leg.features for leg in candidate.legs if leg.side == "BUY"]
    if any(feature.is_iv_outlier for feature in buy_features):
        warnings.append("buy leg IV outlier")
        return 0.0
    ranks = [feature.iv_rank_90d for feature in buy_features if feature.iv_rank_90d is not None]
    if not ranks:
        return 6.0
    avg_rank = sum(ranks) / len(ranks)
    if candidate.strategy_type in {"LONG_CALL", "LONG_PUT"} and avg_rank > 80:
        warnings.append("high IV rank for long premium")
        return 2.0
    if 20 <= avg_rank <= 70:
        return 10.0
    return 6.0


def _reward_risk_score(candidate: OptionStrategyCandidate, warnings: list[str]) -> float:
    if candidate.reward_risk is None:
        return 8.0
    if candidate.reward_risk >= 1.5:
        return 15.0
    if candidate.reward_risk >= 1.0:
        return 9.0
    warnings.append("reward/risk below 1")
    return 0.0


def _expiry_fit_score(candidate: OptionStrategyCandidate, warnings: list[str]) -> float:
    if not candidate.legs:
        return 0.0
    min_dte = min(leg.features.days_to_expiry for leg in candidate.legs)
    if min_dte <= candidate.expected_holding_days + 2:
        warnings.append("expiry too close to expected holding period")
        return 0.0
    if candidate.strategy_type in {"LONG_CALL", "LONG_PUT"} and 20 <= min_dte <= 60:
        return 5.0
    if candidate.strategy_type in {"BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"} and 7 <= min_dte <= 30:
        return 5.0
    return 3.0


def _confidence(score: float) -> SelectionConfidence:
    if score >= 80:
        return "HIGH"
    if score >= 65:
        return "MEDIUM"
    return "LOW"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output
