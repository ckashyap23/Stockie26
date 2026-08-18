from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from src.common.config import (
    get_paper_capital_per_trade_pct, get_paper_trading_capital, get_settings,
    get_sl_pct, get_target_pct,
)
from src.execution.position_sizing import size_long_option_position
from src.data_manager.db.client_factory import get_database_client


def load_paper_executed_trades(
    underlying: str = "NIFTY",
    model_version: str = "cascade_v1",
    mode: str = "paper",
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """Load actual executed paper trades from PaperExecutionSignal + PaperTradeResult.

    Returns CLOSED trades (with entry + exit fills) and OPEN trades (entry only).
    Filtered by paper_trade_date (the date the trade was physically entered).

    mode='live' is not yet supported — live execution tables don't exist yet.
    """
    if mode != "paper":
        raise NotImplementedError(
            f"mode={mode!r} is not yet supported; only 'paper' execution tables exist. "
            "Switch to live trading tables when live mode is implemented."
        )

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        params: list[Any] = [underlying.upper(), model_version]
        date_filter = ""
        if start_date is not None:
            date_filter += " AND s.paper_trade_date >= %s"
            params.append(start_date)
        if end_date is not None:
            date_filter += " AND s.paper_trade_date <= %s"
            params.append(end_date)

        sql = f"""
            SELECT
                s.id            AS signal_id,
                s.symbol,
                s.model_version,
                s.signal_trade_date,
                s.paper_trade_date,
                s.direction,
                s.selected_strategy,
                s.prediction_strategy,
                s.option_symbol,
                s.option_token,
                s.option_type,
                s.quantity,
                s.lot_size,
                s.planned_entry_price,
                r.entry_price,
                r.entry_time,
                r.exit_price,
                r.exit_time,
                r.exit_reason,
                r.pnl_points,
                r.pnl_per_lot,
                r.return_pct,
                r.entry_charges,
                r.exit_charges,
                r.total_charges,
                r.net_pnl_per_lot,
                r.net_return_pct,
                r.status       AS trade_status
            FROM "PaperExecutionSignal" s
            JOIN "PaperTradeResult" r
              ON r.paper_execution_signal_id = s.id
            LEFT JOIN "NiftyPrediction" p
              ON p.symbol = s.symbol
             AND p.model_version = s.model_version
             AND p.signal_date = s.signal_trade_date
            WHERE UPPER(s.symbol) = %s
              AND s.model_version = %s
              AND s.status IN ('OPEN', 'CLOSED')
              {date_filter}
            ORDER BY s.paper_trade_date, s.signal_trade_date, s.id
        """
        with db.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
    finally:
        db.close()

    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df

    for col in ("signal_trade_date", "paper_trade_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col]).dt.date

    df["trade_id"] = df.apply(
        lambda row: f"{row['paper_trade_date']}_{int(row['option_token'])}",
        axis=1,
    )
    return apply_current_policy_levels(df)


def apply_current_policy_levels(trades: pd.DataFrame) -> pd.DataFrame:
    """Add current env target/SL levels without altering historical fills/exits."""
    if trades.empty:
        return trades

    out = trades.copy()
    out["target_1_pct"] = get_target_pct()
    out["stop_loss_pct"] = get_sl_pct()
    entry = pd.to_numeric(out["entry_price"], errors="coerce")
    out["target_1_price"] = entry * (1 + out["target_1_pct"])
    out["stop_loss_price"] = entry * (1 - out["stop_loss_pct"])
    lot_size = pd.to_numeric(out.get("lot_size"), errors="coerce")
    sized = [
        size_long_option_position(
            float(entry.loc[idx]), int(lot_size.loc[idx]),
            get_paper_trading_capital(), get_paper_capital_per_trade_pct(),
        )
        for idx in out.index
    ]
    out["lot_count"] = [value[0] for value in sized]
    out["quantity"] = [value[1] for value in sized]
    quantity = pd.to_numeric(out["quantity"], errors="coerce")
    pnl_points = pd.to_numeric(out.get("pnl_points"), errors="coerce")
    out["gross_pnl"] = pnl_points * quantity
    charges = pd.to_numeric(out.get("total_charges"), errors="coerce").fillna(0)
    out["net_pnl"] = out["gross_pnl"] - charges
    return out
