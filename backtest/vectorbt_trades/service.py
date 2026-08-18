from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from backtest.vectorbt_trades.data_adapter import load_paper_executed_trades
from backtest.vectorbt_trades.runner import build_signal_matrices_from_fills, run_vectorbt_or_fallback
from backtest.vectorbt_trades.schemas import StockieVectorBTRequest, StockieVectorBTResult


def run_stockie_vectorbt_backtest(request: StockieVectorBTRequest) -> StockieVectorBTResult:
    all_trades = load_paper_executed_trades(
        underlying=request.underlying,
        model_version=request.model_version,
        mode=request.mode,
        start_date=request.start_date,
        end_date=request.end_date,
    )

    closed_trades, open_trades = _split_by_status(all_trades)

    price, entries, exits = build_signal_matrices_from_fills(closed_trades)
    trades, metrics, used_vectorbt = run_vectorbt_or_fallback(
        price=price,
        entries=entries,
        exits=exits,
        closed_trades=closed_trades,
        initial_cash=request.initial_cash,
        fees=request.fees,
        slippage=request.slippage,
    )
    enriched_trades = _enrich_trades(trades, closed_trades, used_vectorbt)
    output_paths = write_outputs(
        output_dir=request.output_dir,
        all_trades=all_trades,
        closed_trades=closed_trades,
        open_trades=open_trades,
        trades=enriched_trades,
        metrics=metrics,
        used_vectorbt=used_vectorbt,
        mode=request.mode,
    )
    return StockieVectorBTResult(
        trade_plans=all_trades,
        price=price,
        entries=entries,
        exits=exits,
        trades=enriched_trades,
        metrics=metrics,
        used_vectorbt=used_vectorbt,
        output_paths=output_paths,
    )


def _split_by_status(all_trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if all_trades.empty or "trade_status" not in all_trades.columns:
        return all_trades.iloc[0:0].copy(), all_trades.iloc[0:0].copy()
    closed = all_trades[all_trades["trade_status"] == "CLOSED"].reset_index(drop=True)
    open_ = all_trades[all_trades["trade_status"] == "OPEN"].reset_index(drop=True)
    return closed, open_


def _enrich_trades(
    trades: pd.DataFrame,
    closed_trades: pd.DataFrame,
    used_vectorbt: bool,
) -> pd.DataFrame:
    if trades.empty or closed_trades.empty:
        return trades

    out = trades.copy()
    if "Direction" in out.columns:
        out = out.rename(columns={"Direction": "vectorbt_direction"})
    out = out.drop(columns=[
        "Entry Fees", "Exit Fees", "Avg Entry Price", "Avg Exit Price",
        "entry_price", "exit_price",
    ], errors="ignore")

    if used_vectorbt and "Column" in out.columns:
        # VectorBT column IDs are positional â€” map back to trade_ids
        trade_ids = list(closed_trades["trade_id"])
        out["trade_id"] = out["Column"].apply(
            lambda v: trade_ids[int(v)] if str(v).isdigit() and int(v) < len(trade_ids) else str(v)
        )

    merge_cols = [
        "trade_id", "paper_trade_date", "signal_trade_date", "direction",
        "option_symbol", "option_type", "quantity", "lot_size", "lot_count", "selected_strategy",
        "prediction_strategy", "entry_price", "exit_price",
        "target_1_pct", "target_1_price", "stop_loss_pct", "stop_loss_price",
        "exit_reason", "pnl_per_lot", "return_pct",
        "entry_charges", "exit_charges", "total_charges",
        "net_pnl_per_lot", "net_return_pct", "gross_pnl", "net_pnl",
    ]
    available = [c for c in merge_cols if c in closed_trades.columns]
    out = out.merge(closed_trades[available], on="trade_id", how="left")
    return out


def write_outputs(
    output_dir: Path,
    all_trades: pd.DataFrame,
    closed_trades: pd.DataFrame,
    open_trades: pd.DataFrame,
    trades: pd.DataFrame,
    metrics: dict[str, Any],
    used_vectorbt: bool,
    mode: str = "paper",
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "all_trades": output_dir / "paper_executed_trades.csv",
        "closed_trades": output_dir / "paper_closed_trades.csv",
        "open_trades": output_dir / "paper_open_trades.csv",
        "trades": output_dir / "vectorbt_trades.csv",
        "summary": output_dir / "vectorbt_summary.txt",
    }
    all_trades.to_csv(paths["all_trades"], index=False)
    closed_trades.to_csv(paths["closed_trades"], index=False)
    open_trades.to_csv(paths["open_trades"], index=False)
    trades.to_csv(paths["trades"], index=False)

    # Actual PnL from DB fills (authoritative)
    pnl_series = pd.to_numeric(closed_trades.get("pnl_per_lot", pd.Series(dtype=float)), errors="coerce").fillna(0)
    total_pnl_actual = float(pnl_series.sum())
    charges_series = pd.to_numeric(closed_trades.get("total_charges", pd.Series(dtype=float)), errors="coerce").fillna(0)
    net_pnl_series = pd.to_numeric(closed_trades.get("net_pnl_per_lot", pd.Series(dtype=float)), errors="coerce")
    total_charges = float(charges_series.sum())
    total_net_pnl = float(net_pnl_series.sum()) if net_pnl_series.notna().any() else None
    n_closed = len(closed_trades)
    n_open = len(open_trades)

    lines = [
        "Stockie VectorBT backtest summary",
        "",
        f"source:  PaperTradeResult (actual executed fills)",
        f"engine:  {'vectorbt' if used_vectorbt else 'pandas_fallback'}",
        f"mode:    {mode}",
        f"trades loaded:   {len(all_trades)} total  ({n_closed} closed, {n_open} open/not yet closed)",
        f"replayed trades: {len(trades)}",
        "",
        "--- Actual PnL (from DB fills, authoritative) ---",
        f"  gross_pnl_per_lot: {round(total_pnl_actual, 2)}",
        f"  Kite_charges:      {round(total_charges, 2)}",
        f"  net_pnl_per_lot:   {round(total_net_pnl, 2) if total_net_pnl is not None else 'N/A'}",
        "",
        "--- Portfolio metrics (vectorbt normalized sizing) ---",
    ]
    for key, value in metrics.items():
        lines.append(f"  {key}: {value}")
    if used_vectorbt:
        lines += [
            "",
            "Note: vectorbt metrics use normalized position sizing (initial_cash / entry_price).",
            "      For actual lot-based PnL refer to pnl_per_lot column and the 'Actual PnL' section above.",
        ]
    paths["summary"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths

