from __future__ import annotations


from technical_analysis_service.analysis.prediction.schema import UnderlyingView

from .schema import OptionBias


def derive_option_bias(view: UnderlyingView) -> OptionBias:
    prediction_side = _prediction_side(view)
    if prediction_side == "NO_POSITION" or view.strength_score < 65:
        return "NEUTRAL"

    if prediction_side == "CALL":
        bias: OptionBias = "BULLISH_STRONG" if view.strength_score >= 80 else "BULLISH_MODERATE"
    elif prediction_side == "PUT":
        bias = "BEARISH_STRONG" if view.strength_score >= 80 else "BEARISH_MODERATE"
    else:
        return "NEUTRAL"

    if _opposes(view.sector_regime, view.direction):
        bias = _downgrade(bias)
    if _opposes(view.benchmark_regime, view.direction):
        bias = _downgrade(bias)
    if view.expected_move_pct is None and view.atr14 is None:
        bias = _downgrade(bias)
    return bias


def _opposes(regime: str | None, direction: str) -> bool:
    return (direction == "BULLISH" and regime == "TREND_DOWN") or (
        direction == "BEARISH" and regime == "TREND_UP"
    )


def _downgrade(bias: OptionBias) -> OptionBias:
    mapping: dict[OptionBias, OptionBias] = {
        "BULLISH_STRONG": "BULLISH_MODERATE",
        "BULLISH_MODERATE": "NEUTRAL",
        "BEARISH_STRONG": "BEARISH_MODERATE",
        "BEARISH_MODERATE": "NEUTRAL",
        "NEUTRAL": "NEUTRAL",
    }
    return mapping[bias]


def _prediction_side(view: UnderlyingView) -> str:
    direction = str(view.direction)
    if direction in {"CALL", "PUT", "NO_POSITION"}:
        return direction
    raw_signal = str(view.raw_signal)
    if raw_signal in {"CALL", "PUT"}:
        return raw_signal
    if direction == "BULLISH":
        return "CALL"
    if direction == "BEARISH":
        return "PUT"
    return "NO_POSITION"
