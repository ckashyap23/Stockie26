from __future__ import annotations


from .schema import OptionBias, OptionStrategyType

HIGH_IV_RANK = 70
VERY_HIGH_IV_RANK = 85


def choose_option_strategy_type(
    option_bias: OptionBias,
    atm_iv_rank_90d: float | None,
    atm_iv_percentile_90d: float | None,
    expected_move_pct: float | None,
    expected_holding_days: int,
    min_days_to_expiry_available: int | None,
) -> OptionStrategyType:
    _ = atm_iv_percentile_90d, expected_move_pct, expected_holding_days, min_days_to_expiry_available
    if option_bias == "NEUTRAL":
        return "NO_TRADE"

    _ = atm_iv_rank_90d, HIGH_IV_RANK, VERY_HIGH_IV_RANK

    if option_bias in {"BULLISH_STRONG", "BULLISH_MODERATE"}:
        return "LONG_CALL"
    if option_bias in {"BEARISH_STRONG", "BEARISH_MODERATE"}:
        return "LONG_PUT"
    return "NO_TRADE"
