from __future__ import annotations


from .schema import Regime, SetupType, UnderlyingFeatureSnapshot


def estimate_expected_move(
    features: UnderlyingFeatureSnapshot,
    stock_regime: Regime,
    setup_type: SetupType,
) -> tuple[float | None, float | None, int]:
    if stock_regime == "CHOPPY" or setup_type == "NO_SETUP":
        return 0.0, 0.0, 0
    if features.atr14 is None or features.atr14 <= 0 or features.close is None or features.close <= 0:
        return None, None, _holding_days(setup_type)

    multiplier = _regime_multiplier(stock_regime, setup_type)
    expected_move_abs = features.atr14 * multiplier
    expected_move_pct = expected_move_abs / features.close
    return expected_move_pct, expected_move_abs, _holding_days(setup_type)


def _regime_multiplier(stock_regime: Regime, setup_type: SetupType) -> float:
    if stock_regime == "CHOPPY":
        return 0.0
    if setup_type in {"TREND_UP_BREAKOUT_LONG", "TREND_DOWN_BREAKDOWN_SHORT"}:
        return 1.25
    if setup_type in {"TREND_UP_PULLBACK_LONG", "TREND_DOWN_RALLY_SHORT"}:
        return 1.0
    if setup_type in {"RANGE_LOWER_BAND_LONG", "RANGE_UPPER_BAND_SHORT"}:
        return 0.75
    return 0.0


def _holding_days(setup_type: SetupType) -> int:
    if setup_type in {
        "TREND_UP_PULLBACK_LONG",
        "TREND_UP_BREAKOUT_LONG",
        "TREND_DOWN_RALLY_SHORT",
        "TREND_DOWN_BREAKDOWN_SHORT",
    }:
        return 3
    if setup_type in {"RANGE_LOWER_BAND_LONG", "RANGE_UPPER_BAND_SHORT"}:
        return 2
    return 0
