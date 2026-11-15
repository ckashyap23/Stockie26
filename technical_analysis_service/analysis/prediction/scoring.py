from __future__ import annotations


from dataclasses import dataclass

from .schema import Confidence, Direction, RegimeSnapshot, SetupType, UnderlyingFeatureSnapshot


@dataclass(frozen=True)
class ScoreBreakdown:
    stock_technical_score: float
    sector_confirmation_score: float
    benchmark_confirmation_score: float
    relative_strength_score: float
    volume_confirmation_score: float
    risk_quality_score: float
    regime_quality_score: float
    penalty_score: float
    warnings: list[str]

    @property
    def final_score(self) -> float:
        raw = (
            self.stock_technical_score
            + self.sector_confirmation_score
            + self.benchmark_confirmation_score
            + self.relative_strength_score
            + self.volume_confirmation_score
            + self.risk_quality_score
            + self.regime_quality_score
            - self.penalty_score
        )
        return max(0.0, min(100.0, round(raw, 4)))


def confidence_from_score(score: float) -> Confidence:
    if score >= 80:
        return "HIGH"
    if score >= 65:
        return "MEDIUM"
    return "LOW"


def score_underlying_view(
    features: UnderlyingFeatureSnapshot,
    regime: RegimeSnapshot,
    direction: Direction,
    setup_type: SetupType,
    reward_risk: float | None = None,
) -> ScoreBreakdown:
    warnings: list[str] = []
    if direction == "NEUTRAL":
        return ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0, ["Neutral direction has no tradable score"])

    stock_score = _score_stock_technicals(features, direction, setup_type, warnings)
    sector_score = _score_sector(regime, features, direction, warnings)
    benchmark_score = _score_benchmark(regime, direction, warnings)
    relative_strength_score = _score_relative_strength(features, direction, warnings)
    volume_score = _score_volume(features, setup_type, warnings)
    risk_score = _score_risk(features, reward_risk, warnings)
    regime_score = _score_regime_quality(regime, direction, setup_type, warnings)
    penalty = _penalty_score(features, regime, setup_type, warnings)
    return ScoreBreakdown(
        stock_technical_score=stock_score,
        sector_confirmation_score=sector_score,
        benchmark_confirmation_score=benchmark_score,
        relative_strength_score=relative_strength_score,
        volume_confirmation_score=volume_score,
        risk_quality_score=risk_score,
        regime_quality_score=regime_score,
        penalty_score=penalty,
        warnings=warnings,
    )


def _score_stock_technicals(
    f: UnderlyingFeatureSnapshot,
    direction: Direction,
    setup_type: SetupType,
    warnings: list[str],
) -> float:
    score = 0.0
    if direction == "BULLISH":
        if setup_type == "RANGE_LOWER_BAND_LONG":
            # RANGE reversal: grade the oversold entry conditions, not trend-following alignment.
            # close < bb_lower is the primary signal; low RSI confirms oversold.
            score += _points(f.rsi14 is not None and f.rsi14 <= 40, 10)
            score += _points(f.rsi14 is not None and f.rsi14 <= 30, 5)   # deep oversold bonus
            score += _points(f.close is not None and f.bb_lower is not None and f.close <= f.bb_lower, 5)
        else:
            score += _points(f.close is not None and f.ma20 is not None and f.close > f.ma20, 5)
            score += _points(f.ma20 is not None and f.ma50 is not None and f.ma20 > f.ma50, 5)
            score += _points(f.ma20_slope is not None and f.ma20_slope > 0, 5)
            score += _points(f.ma50_slope is not None and f.ma50_slope > 0, 5)
            score += _points(f.rsi14 is not None and 45 <= f.rsi14 <= 70, 5)
    elif direction == "BEARISH":
        if setup_type == "RANGE_UPPER_BAND_SHORT":
            # RANGE reversal from top: grade overbought conditions.
            score += _points(f.rsi14 is not None and f.rsi14 >= 60, 10)
            score += _points(f.rsi14 is not None and f.rsi14 >= 70, 5)   # deep overbought bonus
            score += _points(f.close is not None and f.bb_upper is not None and f.close >= f.bb_upper, 5)
        else:
            score += _points(f.close is not None and f.ma20 is not None and f.close < f.ma20, 5)
            score += _points(f.ma20 is not None and f.ma50 is not None and f.ma20 < f.ma50, 5)
            score += _points(f.ma20_slope is not None and f.ma20_slope < 0, 5)
            score += _points(f.ma50_slope is not None and f.ma50_slope < 0, 5)
            score += _points(f.rsi14 is not None and 30 <= f.rsi14 <= 55, 5)
    if setup_type == "NO_SETUP":
        warnings.append("No concrete setup type was identified")
    return score


def _score_sector(
    regime: RegimeSnapshot,
    f: UnderlyingFeatureSnapshot,
    direction: Direction,
    warnings: list[str],
) -> float:
    if regime.sector_regime is None:
        warnings.append("Missing sector regime confirmation")
        return 0.0
    if _regime_supports(regime.sector_regime, direction):
        return 10.0 + _points(_relative_supports(f.relative_strength_vs_sector, direction), 5)
    if _regime_opposes(regime.sector_regime, direction):
        warnings.append("Sector trend conflicts with stock signal")
        return -10.0
    return 3.0


def _score_benchmark(regime: RegimeSnapshot, direction: Direction, warnings: list[str]) -> float:
    if regime.benchmark_regime is None:
        warnings.append("Missing benchmark regime confirmation")
        return 0.0
    if _regime_supports(regime.benchmark_regime, direction):
        return 7.0
    if regime.benchmark_regime == "RANGE":
        return 3.0
    if regime.benchmark_regime == "CHOPPY":
        warnings.append("Benchmark regime is choppy")
        return -5.0
    warnings.append("Benchmark trend conflicts with stock signal")
    return -7.0


def _score_relative_strength(
    f: UnderlyingFeatureSnapshot,
    direction: Direction,
    warnings: list[str],
) -> float:
    score = 0.0
    if f.relative_strength_vs_sector is None:
        warnings.append("Missing relative strength versus sector")
    if f.relative_strength_vs_benchmark is None:
        warnings.append("Missing relative strength versus benchmark")
    score += _points(_relative_supports(f.relative_strength_vs_sector, direction), 7)
    score += _points(_relative_supports(f.relative_strength_vs_benchmark, direction), 5)
    score += _points(_relative_supports(f.ret_20d, direction), 3)
    return score


def _score_volume(f: UnderlyingFeatureSnapshot, setup_type: SetupType, warnings: list[str]) -> float:
    if f.volume_ratio is None:
        warnings.append("Missing volume ratio")
        return 0.0
    if f.volume_ratio >= 1.5:
        return 10.0
    if f.volume_ratio >= 1.1:
        return 7.0
    if f.volume_ratio >= 0.8:
        return 4.0
    if setup_type in {"TREND_UP_BREAKOUT_LONG", "TREND_DOWN_BREAKDOWN_SHORT"}:
        warnings.append("Breakout signal has weak volume confirmation")
    return 0.0


def _score_risk(f: UnderlyingFeatureSnapshot, reward_risk: float | None, warnings: list[str]) -> float:
    score = 0.0
    if f.atr14 is not None and f.close is not None and f.close > 0:
        atr_pct = f.atr14 / f.close
        if atr_pct <= 0.04:
            score += 4
        else:
            warnings.append("ATR is elevated, stop distance may be wide")
    else:
        warnings.append("Missing ATR or close for risk quality")
    if f.volatility_20d is not None and f.volatility_20d <= 0.035:
        score += 3
    elif f.volatility_20d is not None:
        warnings.append("Volatility is elevated")
    if reward_risk is None or reward_risk >= 1.5:
        score += 3
    else:
        warnings.append("Reward/risk is below threshold")
    return score


def _score_regime_quality(
    regime: RegimeSnapshot,
    direction: Direction,
    setup_type: SetupType,
    warnings: list[str],
) -> float:
    if regime.stock_regime == "CHOPPY":
        warnings.append("Stock regime is CHOPPY")
        return -15.0
    if setup_type in {"RANGE_LOWER_BAND_LONG", "RANGE_UPPER_BAND_SHORT"} and regime.stock_regime == "RANGE":
        return 10.0
    if _regime_supports(regime.stock_regime, direction):
        if regime.sector_regime and _regime_supports(regime.sector_regime, direction):
            return 15.0
        if regime.sector_regime == "RANGE":
            return 10.0
        return 8.0
    return 0.0


def _penalty_score(
    f: UnderlyingFeatureSnapshot,
    regime: RegimeSnapshot,
    setup_type: SetupType,
    warnings: list[str],
) -> float:
    penalty = 0.0
    if regime.stock_regime == "CHOPPY":
        penalty += 20
    if regime.sector_regime == "CHOPPY":
        penalty += 8
    if regime.benchmark_regime == "CHOPPY":
        penalty += 5
    if f.volume_ratio is not None and f.volume_ratio < 0.8 and setup_type in {
        "TREND_UP_BREAKOUT_LONG",
        "TREND_DOWN_BREAKDOWN_SHORT",
    }:
        penalty += 10
    missing = sum(
        value is None
        for value in [
            f.close,
            f.ma20,
            f.ma50,
            f.rsi14,
            f.atr14,
            f.volatility_20d,
        ]
    )
    if missing:
        penalty += min(20, missing * 5)
        warnings.append("Missing critical feature values")
    return penalty


def _points(condition: bool, points: float) -> float:
    return points if condition else 0.0


def _relative_supports(value: float | None, direction: Direction) -> bool:
    if value is None:
        return False
    return value > 0 if direction == "BULLISH" else value < 0


def _regime_supports(regime: str, direction: Direction) -> bool:
    return (direction == "BULLISH" and regime == "TREND_UP") or (
        direction == "BEARISH" and regime == "TREND_DOWN"
    )


def _regime_opposes(regime: str, direction: Direction) -> bool:
    return (direction == "BULLISH" and regime == "TREND_DOWN") or (
        direction == "BEARISH" and regime == "TREND_UP"
    )
