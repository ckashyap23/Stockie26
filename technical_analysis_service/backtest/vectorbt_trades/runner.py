from __future__ import annotations


from typing import Any

import pandas as pd


def build_signal_matrices_from_fills(
    closed_trades: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build VectorBT price/entry/exit matrices from actual paper trade fills.

    Each trade becomes a column keyed by trade_id. The price series is a
    synthetic 2-point series: entry_time → entry_price, exit_time → exit_price.
    No option snapshot lookup is needed — exits are already recorded in
    PaperTradeResult with actual fill prices.
    """
    if closed_trades.empty:
        empty = pd.DataFrame()
        return empty, empty, empty

    required = {"trade_id", "entry_time", "entry_price", "exit_time", "exit_price"}
    missing = required - set(closed_trades.columns)
    if missing:
        raise ValueError(f"Missing required columns for fills-based replay: {missing}")

    entry_times = pd.to_datetime(closed_trades["entry_time"])
    exit_times = pd.to_datetime(closed_trades["exit_time"])
    all_times = pd.DatetimeIndex(sorted(set(entry_times) | set(exit_times)))

    trade_ids = list(closed_trades["trade_id"])
    price = pd.DataFrame(index=all_times, columns=trade_ids, dtype=float)
    entries = pd.DataFrame(False, index=all_times, columns=trade_ids)
    exits = pd.DataFrame(False, index=all_times, columns=trade_ids)

    for _, row in closed_trades.iterrows():
        tid = row["trade_id"]
        et = pd.Timestamp(row["entry_time"])
        xt = pd.Timestamp(row["exit_time"])
        ep = float(row["entry_price"])
        xp = float(row["exit_price"])

        price.loc[et, tid] = ep
        price.loc[xt, tid] = xp
        price[tid] = price[tid].ffill().bfill()
        entries.loc[et, tid] = True
        exits.loc[xt, tid] = True

    return price, entries, exits


def run_vectorbt_or_fallback(
    price: pd.DataFrame,
    entries: pd.DataFrame,
    exits: pd.DataFrame,
    initial_cash: float,
    fees: float,
    slippage: float,
    closed_trades: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], bool]:
    """Run VectorBT when installed; otherwise build metrics directly from fills."""
    replay_trades = closed_trades if closed_trades is not None else pd.DataFrame()
    if price.empty:
        trades, metrics = _fallback_from_fills(replay_trades, fees, slippage)
        return trades, metrics, False

    try:
        import vectorbt as vbt  # type: ignore
    except Exception:
        trades, metrics = _fallback_from_fills(replay_trades, fees, slippage)
        return trades, metrics, False

    portfolio = vbt.Portfolio.from_signals(
        close=price,
        entries=entries,
        exits=exits,
        init_cash=initial_cash,
        fees=fees,
        slippage=slippage,
        direction="longonly",
    )
    trades = portfolio.trades.records_readable.copy()
    metrics = portfolio.stats().to_dict()
    return trades, metrics, True


def _fallback_from_fills(
    closed_trades: pd.DataFrame,
    fees: float,
    slippage: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Compute trade metrics directly from actual fill prices without price simulation."""
    if closed_trades.empty:
        return pd.DataFrame(), {"trades": 0, "total_pnl_per_lot": 0.0, "win_rate_pct": None}

    rows: list[dict[str, Any]] = []
    for _, row in closed_trades.iterrows():
        ep = _float_or_none(row.get("entry_price"))
        xp = _float_or_none(row.get("exit_price"))
        ls = _float_or_none(row.get("lot_size")) or 1.0
        if ep is None or xp is None:
            continue
        fill_entry = ep * (1 + slippage)
        fill_exit = xp * (1 - slippage)
        fee_cost = (fill_entry + fill_exit) * fees
        net_pnl_unit = (fill_exit - fill_entry) - fee_cost
        rows.append({
            "trade_id": row.get("trade_id", ""),
            "entry_time": row.get("entry_time"),
            "exit_time": row.get("exit_time"),
            "entry_price": round(fill_entry, 4),
            "exit_price": round(fill_exit, 4),
            "pnl_per_unit": round(net_pnl_unit, 4),
            "pnl_per_lot": round(net_pnl_unit * ls, 2),
            "return_pct": round(net_pnl_unit / fill_entry * 100, 4) if fill_entry else None,
            "exit_reason": row.get("exit_reason"),
        })

    trades = pd.DataFrame(rows)
    pnl = pd.to_numeric(trades.get("pnl_per_lot", pd.Series(dtype=float)), errors="coerce").fillna(0)
    n = len(trades)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    metrics: dict[str, Any] = {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "total_pnl_per_lot": round(float(pnl.sum()), 2) if n else 0.0,
        "avg_pnl_per_lot": round(float(pnl.mean()), 2) if n else None,
        "win_rate_pct": round(wins / n * 100, 2) if n else None,
    }
    return trades, metrics


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
