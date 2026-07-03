from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client


def _default_trade_date() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _fmt_pnl(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.2f}"


def _section(title: str, df: pd.DataFrame, pnl_col: str, extra_cols: list[str]) -> list[str]:
    lines = [f"{'='*60}", f"  {title} ({len(df)} position(s))", f"{'='*60}"]
    if df.empty:
        lines.append("  (none)")
        return lines
    for _, row in df.iterrows():
        pnl = pd.to_numeric(row.get(pnl_col), errors="coerce")
        extra = "  ".join(
            f"{c}={row.get(c, '')}" for c in extra_cols if row.get(c) not in (None, "")
        )
        lines.append(
            f"  {row.get('option_symbol', '?'):30s}  "
            f"dir={row.get('trade_direction', '?'):4s}  "
            f"entry={row.get('entry_price', '?')}  "
            f"pnl/lot={_fmt_pnl(float(pnl) if pd.notna(pnl) else None)}"
            + (f"  {extra}" if extra else "")
        )
    pnl_series = pd.to_numeric(df[pnl_col], errors="coerce") if pnl_col in df.columns else pd.Series(dtype=float)
    total = float(pnl_series.sum())
    lines.append(f"  {'-'*40}")
    lines.append(f"  subtotal pnl/lot: {_fmt_pnl(total)}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "End-of-day paper trade report. Shows open (unrealized MTM) and closed "
            "(realized) positions. Source of truth: PaperTradeResult table in DB."
        )
    )
    parser.add_argument("--trade-date", default=None, help="Paper trade date YYYY-MM-DD. Default: today IST")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--model-version", default="cascade_v1", help="Prediction model version. Default: cascade_v1")
    parser.add_argument(
        "--output-dir",
        default=str(Path("output") / "backtest" / "NIFTY" / "paper"),
        help="Output directory. Default: output/backtest/NIFTY/paper",
    )
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date) if args.trade_date else _default_trade_date()
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        rows = db.list_paper_trade_results(
            trade_date=trade_date,
            statuses=("OPEN", "CLOSED", "PLANNED", "FAILED"),
            symbol=args.underlying.upper(),
            model_version=args.model_version,
        )
    finally:
        db.close()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{trade_date.isoformat()}_paper_trades.csv"
    summary_path = output_dir / f"{trade_date.isoformat()}_paper_summary.txt"

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)

    # Split by status
    def _status(s: str) -> pd.DataFrame:
        if df.empty or "trade_status" not in df.columns:
            return pd.DataFrame()
        return df[df["trade_status"] == s].reset_index(drop=True)

    open_df = _status("OPEN")
    closed_df = _status("CLOSED")
    planned_df = _status("PLANNED")
    failed_df = _status("FAILED")

    # PnL computation: gross is retained for audit; net includes Kite charges.
    open_pnl_series = pd.to_numeric(open_df.get("pnl_per_lot", pd.Series(dtype=float)), errors="coerce").fillna(0)
    closed_pnl_series = pd.to_numeric(closed_df.get("pnl_per_lot", pd.Series(dtype=float)), errors="coerce").fillna(0)
    open_net_series = pd.to_numeric(open_df.get("net_pnl_per_lot", pd.Series(dtype=float)), errors="coerce")
    closed_net_series = pd.to_numeric(closed_df.get("net_pnl_per_lot", pd.Series(dtype=float)), errors="coerce")
    charges_series = pd.to_numeric(df.get("total_charges", pd.Series(dtype=float)), errors="coerce").fillna(0)
    open_pnl_total = float(open_pnl_series.sum())
    closed_pnl_total = float(closed_pnl_series.sum())
    total_pnl = open_pnl_total + closed_pnl_total
    open_net_total = float(open_net_series.sum()) if open_net_series.notna().any() else None
    closed_net_total = float(closed_net_series.sum()) if closed_net_series.notna().any() else None
    net_total = (
        (open_net_total or 0.0) + (closed_net_total or 0.0)
        if open_net_total is not None or closed_net_total is not None else None
    )
    total_charges = float(charges_series.sum())

    wins = int((closed_pnl_series > 0).sum())
    losses = int((closed_pnl_series < 0).sum())
    win_rate = round(wins / len(closed_df) * 100, 1) if len(closed_df) > 0 else None

    lines: list[str] = [
        f"Stockie paper report — {trade_date.isoformat()}",
        f"underlying: {args.underlying.upper()}  model: {args.model_version}",
        "",
    ]

    # Open positions — unrealized MTM PnL from last monitor cycle
    lines += _section(
        "OPEN positions (unrealized MTM PnL from last monitor run)",
        open_df,
        pnl_col="net_pnl_per_lot",
        extra_cols=["current_price", "current_quote_time", "entry_charges", "total_charges"],
    )
    lines.append("")

    # Closed positions — realized PnL
    lines += _section(
        "CLOSED positions (realized PnL)",
        closed_df,
        pnl_col="net_pnl_per_lot",
        extra_cols=["exit_price", "exit_reason", "entry_charges", "exit_charges", "total_charges"],
    )
    lines.append("")

    # Pending / failed
    if not planned_df.empty:
        lines.append(f"PLANNED (not yet entered): {len(planned_df)}")
    if not failed_df.empty:
        lines.append(f"FAILED entries: {len(failed_df)}")
    if not planned_df.empty or not failed_df.empty:
        lines.append("")

    # Summary
    lines += [
        "=" * 60,
        "  SUMMARY",
        "=" * 60,
        f"  open positions:          {len(open_df)}",
        f"  closed positions:        {len(closed_df)}",
        f"  gross unrealized PnL:    {_fmt_pnl(open_pnl_total)}",
        f"  gross realized PnL:      {_fmt_pnl(closed_pnl_total)}",
        f"  gross total PnL:         {_fmt_pnl(total_pnl)}",
        f"  Kite charges:            {_fmt_pnl(-total_charges)}",
        f"  net unrealized PnL:      {_fmt_pnl(open_net_total)}",
        f"  net realized PnL:        {_fmt_pnl(closed_net_total)}",
        f"  net total PnL:           {_fmt_pnl(net_total)}",
    ]
    if len(closed_df) > 0:
        lines.append(f"  win/loss:                {wins}W / {losses}L  ({win_rate}% win rate)")
    lines += [
        "",
        f"  csv:     {csv_path}",
        f"  summary: {summary_path}",
        "",
        "Note: Gross and net PnL source is PaperTradeResult. Net PnL requires successful Kite charge calculation.",
        "      Entry charges are captured by daily_paper_entry; exit charges by daily_paper_monitor on close.",
        "      Production pipeline (NiftyPrediction/NiftyOptionSelection) does NOT track execution PnL.",
    ]

    summary_text = "\n".join(lines) + "\n"
    summary_path.write_text(summary_text, encoding="utf-8")
    print(summary_text)
    print({"csv": str(csv_path), "summary": str(summary_path), "rows": len(df)})


if __name__ == "__main__":
    main()
