from __future__ import annotations


from datetime import date
from statistics import median

from .schema import OptionContract, OptionFeatures


def compute_option_features_for_chain(
    contracts: list[OptionContract],
    spot_price: float,
    trade_date: str,
    atm_iv_history_90d: list[float] | None = None,
) -> dict[str, OptionFeatures]:
    by_expiry_type: dict[tuple[str, str], list[OptionContract]] = {}
    by_expiry: dict[str, list[OptionContract]] = {}
    for contract in contracts:
        by_expiry_type.setdefault((contract.expiry, contract.option_type), []).append(contract)
        by_expiry.setdefault(contract.expiry, []).append(contract)

    atm_iv_by_expiry = {
        expiry: _current_atm_iv(expiry_contracts, spot_price)
        for expiry, expiry_contracts in by_expiry.items()
    }

    output: dict[str, OptionFeatures] = {}
    for contract in contracts:
        atm_iv = atm_iv_by_expiry.get(contract.expiry)
        if atm_iv is None:
            atm_iv = _current_atm_iv(_similar_dte_contracts(contract, contracts, trade_date), spot_price)
        output[contract.tradingsymbol] = _compute_features(
            contract,
            spot_price,
            trade_date,
            atm_iv,
            atm_iv_history_90d,
            by_expiry_type.get((contract.expiry, contract.option_type), []),
        )
    return output


def compute_mid_price(bid: float | None, ask: float | None, last_price: float) -> float | None:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return last_price if last_price > 0 else None


def compute_spread_pct(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid


def compute_theta_burn_pct_per_day(theta: float | None, premium: float | None) -> float | None:
    if theta is None or premium is None or premium <= 0:
        return None
    return abs(theta) / premium


def compute_moneyness_pct(spot: float, strike: float) -> float | None:
    if spot <= 0:
        return None
    return strike / spot - 1.0


def _compute_features(
    contract: OptionContract,
    spot_price: float,
    trade_date: str,
    atm_iv: float | None,
    atm_iv_history_90d: list[float] | None,
    neighbors: list[OptionContract],
) -> OptionFeatures:
    mid = compute_mid_price(contract.bid, contract.ask, contract.last_price)
    spread_pct = compute_spread_pct(contract.bid, contract.ask)
    theta_burn = compute_theta_burn_pct_per_day(contract.theta, mid)
    days_to_expiry = max(0, (_parse_date(contract.expiry) - _parse_date(trade_date)).days)
    moneyness = compute_moneyness_pct(spot_price, contract.strike)
    liquidity_score, rejection_reasons = _liquidity_score(contract, spread_pct)
    iv_rank = _iv_rank(contract.iv, atm_iv_history_90d)
    iv_percentile = _iv_percentile(contract.iv, atm_iv_history_90d)
    iv_vs_atm = _relative_diff(contract.iv, atm_iv)
    iv_vs_neighbor = _iv_vs_neighbor_median(contract, neighbors)
    is_iv_outlier = _is_iv_outlier(iv_vs_atm, iv_vs_neighbor)
    is_liquid = liquidity_score >= 60
    is_tradeable = _is_tradeable(contract, days_to_expiry, spread_pct, liquidity_score, is_iv_outlier, rejection_reasons)
    if theta_burn is not None and theta_burn > 0.12:
        is_tradeable = False
        rejection_reasons.append("theta burn above hard limit")

    return OptionFeatures(
        tradingsymbol=contract.tradingsymbol,
        expiry=contract.expiry,
        strike=contract.strike,
        option_type=contract.option_type,
        days_to_expiry=days_to_expiry,
        moneyness_pct=moneyness,
        distance_from_spot_pct=abs(moneyness) if moneyness is not None else None,
        iv=contract.iv,
        delta=contract.delta,
        gamma=contract.gamma,
        theta=contract.theta,
        vega=contract.vega,
        spread_pct=spread_pct,
        mid_price=mid,
        liquidity_score=max(0.0, min(100.0, liquidity_score)),
        theta_burn_pct_per_day=theta_burn,
        iv_rank_90d=iv_rank,
        iv_percentile_90d=iv_percentile,
        iv_vs_atm_pct=iv_vs_atm,
        iv_vs_neighbor_median_pct=iv_vs_neighbor,
        is_iv_outlier=is_iv_outlier,
        is_liquid=is_liquid,
        is_tradeable=is_tradeable,
        rejection_reasons=rejection_reasons,
    )


def _liquidity_score(contract: OptionContract, spread_pct: float | None) -> tuple[float, list[str]]:
    score = 100.0
    reasons: list[str] = []
    if spread_pct is None:
        score -= 20
        reasons.append("spread missing")
    elif spread_pct > 0.10:
        score -= 50
        reasons.append("spread above 10%")
    elif spread_pct > 0.05:
        score -= 30
        reasons.append("spread above 5%")
    if contract.volume is None or contract.volume <= 0:
        score -= 20
        reasons.append("volume missing or zero")
    if contract.open_interest is None or contract.open_interest <= 0:
        score -= 20
        reasons.append("open interest missing or zero")
    if contract.last_price <= 0:
        score = 0
        reasons.append("last price missing or zero")
    if contract.iv is None:
        score -= 20
        reasons.append("iv missing")
    if contract.delta is None:
        score -= 20
        reasons.append("delta missing")
    return score, reasons


def _is_tradeable(
    contract: OptionContract,
    days_to_expiry: int,
    spread_pct: float | None,
    liquidity_score: float,
    is_iv_outlier: bool,
    rejection_reasons: list[str],
) -> bool:
    ok = True
    if contract.last_price <= 0:
        ok = False
    if days_to_expiry <= 0:
        ok = False
        rejection_reasons.append("expired contract")
    if spread_pct is not None and spread_pct > 0.10:
        ok = False
    if liquidity_score < 50:
        ok = False
    if contract.iv is None:
        ok = False
    if is_iv_outlier:
        rejection_reasons.append("iv outlier")
    return ok


def _current_atm_iv(contracts: list[OptionContract], spot_price: float) -> float | None:
    valid = [c for c in contracts if c.iv is not None]
    if not valid:
        return None
    atm = min(valid, key=lambda c: abs(c.strike - spot_price))
    return atm.iv


def _similar_dte_contracts(
    target_contract: OptionContract,
    contracts: list[OptionContract],
    trade_date: str,
    tolerance_days: int = 7,
) -> list[OptionContract]:
    target_dte = max(0, (_parse_date(target_contract.expiry) - _parse_date(trade_date)).days)
    return [
        contract
        for contract in contracts
        if abs(max(0, (_parse_date(contract.expiry) - _parse_date(trade_date)).days) - target_dte) <= tolerance_days
    ]


def _iv_rank(current_iv: float | None, history: list[float] | None) -> float | None:
    values = [float(v) for v in history or [] if v is not None]
    if current_iv is None or len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    if hi == lo:
        return None
    return (current_iv - lo) / (hi - lo) * 100.0


def _iv_percentile(current_iv: float | None, history: list[float] | None) -> float | None:
    values = [float(v) for v in history or [] if v is not None]
    if current_iv is None or not values:
        return None
    return sum(1 for value in values if value < current_iv) / len(values) * 100.0


def _relative_diff(value: float | None, base: float | None) -> float | None:
    if value is None or base is None or base == 0:
        return None
    return (value - base) / base


def _iv_vs_neighbor_median(contract: OptionContract, neighbors: list[OptionContract]) -> float | None:
    ivs = [c.iv for c in neighbors if c.iv is not None and c.tradingsymbol != contract.tradingsymbol]
    if contract.iv is None or not ivs:
        return None
    med = median(ivs)
    if med == 0:
        return None
    return (contract.iv - med) / med


def _is_iv_outlier(iv_vs_atm: float | None, iv_vs_neighbor: float | None) -> bool:
    checks = [value for value in (iv_vs_atm, iv_vs_neighbor) if value is not None]
    return any(abs(value) > 0.35 for value in checks)


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])
