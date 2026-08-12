from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.execution.paper import enter_due_paper_trades, prepare_paper_signals


def _default_trade_date() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Market-open script: prepare paper signals from today's option selection, "
            "then immediately enter all PLANNED trades using live Kite quotes. "
            "Run daily_nifty_signal.py before this to populate NiftyOptionSelection."
        )
    )
    parser.add_argument("--trade-date", default=None, help="Paper trade date YYYY-MM-DD. Default: today IST")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--model-version", default="cascade_v1", help="Prediction model version. Default: cascade_v1")
    parser.add_argument("--slippage-pct", type=float, default=0.0, help="Entry slippage as decimal. Default: 0")
    parser.add_argument("--max-stale-seconds", type=int, default=300, help="Reject quotes older than this. Default: 300")
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date) if args.trade_date else _default_trade_date()
    symbol = args.underlying.upper()
    model_version = args.model_version

    # Step 1 — copy today's option selections into PaperExecutionSignal
    inserted = prepare_paper_signals(
        trade_date=trade_date,
        symbol=symbol,
        model_version=model_version,
    )
    print({
        "step": "prepare",
        "trade_date": trade_date.isoformat(),
        "underlying": symbol,
        "model_version": model_version,
        "signals_inserted": inserted,
    })

    if inserted == 0:
        print("No new signals to insert — attempting entry on any existing PLANNED signals.")

    # Step 2 — enter all PLANNED signals using live Kite quotes
    entry_result = enter_due_paper_trades(
        trade_date=trade_date,
        symbol=symbol,
        model_version=model_version,
        slippage_pct=args.slippage_pct,
        max_stale_seconds=args.max_stale_seconds,
    )
    print({
        "step": "entry",
        "trade_date": trade_date.isoformat(),
        "underlying": symbol,
        "model_version": model_version,
        **entry_result,
    })


if __name__ == "__main__":
    main()
