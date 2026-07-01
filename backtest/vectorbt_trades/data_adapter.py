from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from src.common.config import get_settings
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
                s.target_1_pct,
                s.target_1_price,
                s.target_2_pct,
                s.target_2_price,
                s.stop_loss_price,
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
    return df
