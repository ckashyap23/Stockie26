from __future__ import annotations


from .candidate_filter import (
    filter_long_call_candidates,
    filter_long_put_candidates,
    filter_spread_buy_leg_candidates,
    filter_spread_sell_leg_candidates,
)
from .schema import OptionContract, OptionFeatures, OptionLeg, OptionStrategyCandidate, OptionStrategyType
from technical_analysis_service.analysis.prediction.schema import UnderlyingView


def build_strategy_candidates(
    strategy_type: OptionStrategyType,
    contracts: list[OptionContract],
    features: dict[str, OptionFeatures],
    underlying_view: UnderlyingView,
    spot_price: float,
) -> list[OptionStrategyCandidate]:
    if strategy_type == "LONG_CALL":
        return _build_long_calls(contracts, features, underlying_view)
    if strategy_type == "LONG_PUT":
        return _build_long_puts(contracts, features, underlying_view)
    if strategy_type == "BULL_CALL_SPREAD":
        return _build_bull_call_spreads(contracts, features, underlying_view, spot_price)
    if strategy_type == "BEAR_PUT_SPREAD":
        return _build_bear_put_spreads(contracts, features, underlying_view, spot_price)
    return []


def _build_long_calls(
    contracts: list[OptionContract],
    features: dict[str, OptionFeatures],
    view: UnderlyingView,
) -> list[OptionStrategyCandidate]:
    output: list[OptionStrategyCandidate] = []
    for contract in filter_long_call_candidates(contracts, features):
        entry = _buy_price(contract)
        if entry is None or entry <= 0:
            continue
        output.append(
            _candidate(
                "LONG_CALL",
                [OptionLeg("BUY", contract, features[contract.tradingsymbol])],
                "BULLISH",
                view,
                entry,
                max_profit=None,
                max_loss=entry,
                breakeven=contract.strike + entry,
                reward_risk=_estimate_long_reward_risk(contract, view, entry),
                reasons=[f"Buy {contract.tradingsymbol} to express bullish view"],
            )
        )
    return output


def _build_long_puts(
    contracts: list[OptionContract],
    features: dict[str, OptionFeatures],
    view: UnderlyingView,
) -> list[OptionStrategyCandidate]:
    output: list[OptionStrategyCandidate] = []
    for contract in filter_long_put_candidates(contracts, features):
        entry = _buy_price(contract)
        if entry is None or entry <= 0:
            continue
        output.append(
            _candidate(
                "LONG_PUT",
                [OptionLeg("BUY", contract, features[contract.tradingsymbol])],
                "BEARISH",
                view,
                entry,
                max_profit=max(0.0, contract.strike - entry),
                max_loss=entry,
                breakeven=contract.strike - entry,
                reward_risk=_estimate_long_reward_risk(contract, view, entry),
                reasons=[f"Buy {contract.tradingsymbol} to express bearish view"],
            )
        )
    return output


def _build_bull_call_spreads(
    contracts: list[OptionContract],
    features: dict[str, OptionFeatures],
    view: UnderlyingView,
    spot_price: float,
) -> list[OptionStrategyCandidate]:
    output: list[OptionStrategyCandidate] = []
    buy_legs = filter_spread_buy_leg_candidates(contracts, features, "CE")
    for buy_leg in buy_legs:
        for sell_leg in filter_spread_sell_leg_candidates(contracts, features, "CE", buy_leg):
            net_debit = _buy_price(buy_leg) - _sell_price(sell_leg)
            spread_width = sell_leg.strike - buy_leg.strike
            candidate = _debit_spread_candidate(
                "BULL_CALL_SPREAD",
                buy_leg,
                sell_leg,
                features,
                "BULLISH",
                view,
                net_debit,
                spread_width,
                buy_leg.strike + net_debit,
                spot_price,
                f"Buy {buy_leg.tradingsymbol}, sell {sell_leg.tradingsymbol}",
            )
            if candidate is not None:
                output.append(candidate)
    return output


def _build_bear_put_spreads(
    contracts: list[OptionContract],
    features: dict[str, OptionFeatures],
    view: UnderlyingView,
    spot_price: float,
) -> list[OptionStrategyCandidate]:
    output: list[OptionStrategyCandidate] = []
    buy_legs = filter_spread_buy_leg_candidates(contracts, features, "PE")
    for buy_leg in buy_legs:
        for sell_leg in filter_spread_sell_leg_candidates(contracts, features, "PE", buy_leg):
            net_debit = _buy_price(buy_leg) - _sell_price(sell_leg)
            spread_width = buy_leg.strike - sell_leg.strike
            candidate = _debit_spread_candidate(
                "BEAR_PUT_SPREAD",
                buy_leg,
                sell_leg,
                features,
                "BEARISH",
                view,
                net_debit,
                spread_width,
                buy_leg.strike - net_debit,
                spot_price,
                f"Buy {buy_leg.tradingsymbol}, sell {sell_leg.tradingsymbol}",
            )
            if candidate is not None:
                output.append(candidate)
    return output


def _debit_spread_candidate(
    strategy_type: OptionStrategyType,
    buy_leg: OptionContract,
    sell_leg: OptionContract,
    features: dict[str, OptionFeatures],
    direction: str,
    view: UnderlyingView,
    net_debit: float,
    spread_width: float,
    breakeven: float,
    spot_price: float,
    reason: str,
) -> OptionStrategyCandidate | None:
    if net_debit <= 0 or spread_width <= 0 or spread_width > spot_price * 0.05:
        return None
    max_profit = spread_width - net_debit
    if max_profit <= 0:
        return None
    reward_risk = max_profit / net_debit
    if reward_risk < 1.0:
        return None
    return _candidate(
        strategy_type,
        [
            OptionLeg("BUY", buy_leg, features[buy_leg.tradingsymbol]),
            OptionLeg("SELL", sell_leg, features[sell_leg.tradingsymbol]),
        ],
        direction,
        view,
        net_debit,
        max_profit=max_profit,
        max_loss=net_debit,
        breakeven=breakeven,
        reward_risk=reward_risk,
        reasons=[reason],
    )


def _candidate(
    strategy_type: OptionStrategyType,
    legs: list[OptionLeg],
    direction: str,
    view: UnderlyingView,
    entry_debit_or_credit: float | None,
    max_profit: float | None,
    max_loss: float | None,
    breakeven: float | None,
    reward_risk: float | None,
    reasons: list[str],
) -> OptionStrategyCandidate:
    return OptionStrategyCandidate(
        strategy_type=strategy_type,
        legs=legs,
        direction=direction,
        expected_underlying_move_pct=view.expected_move_pct,
        expected_underlying_move_abs=view.expected_move_abs,
        expected_holding_days=view.expected_holding_days,
        entry_debit_or_credit=entry_debit_or_credit,
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven=breakeven,
        reward_risk=reward_risk,
        total_delta=None,
        total_gamma=None,
        total_theta=None,
        total_vega=None,
        score=0.0,
        confidence="LOW",
        reasons=reasons,
        warnings=[],
    )


def _buy_price(contract: OptionContract) -> float:
    return contract.ask if contract.ask is not None and contract.ask > 0 else contract.last_price


def _sell_price(contract: OptionContract) -> float:
    return contract.bid if contract.bid is not None and contract.bid > 0 else contract.last_price


def _estimate_long_reward_risk(contract: OptionContract, view: UnderlyingView, entry: float) -> float | None:
    if view.expected_move_abs is None or contract.delta is None or entry <= 0:
        return None
    signed_move = view.expected_move_abs if view.direction == "BULLISH" else -view.expected_move_abs
    gamma = contract.gamma or 0.0
    theta = contract.theta or 0.0
    estimated_change = (
        contract.delta * signed_move
        + 0.5 * gamma * signed_move * signed_move
        + theta * view.expected_holding_days
    )
    if estimated_change <= 0:
        return 0.0
    return estimated_change / entry
