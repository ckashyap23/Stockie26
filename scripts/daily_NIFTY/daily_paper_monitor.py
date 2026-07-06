from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.execution.paper import enter_due_paper_trades, monitor_open_paper_trades


def _default_trade_date() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor open Stockie paper trades and close on target, stop, or time exit."
    )
    parser.add_argument(
        "--trade-date", default=None,
        help="Optional original paper trade date filter. Default: monitor every OPEN position.",
    )
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--model-version", default="cascade_v1", help="Prediction model version. Default: cascade_v1")
    parser.add_argument("--slippage-pct", type=float, default=0.0, help="Exit slippage as decimal. Default: 0")
    parser.add_argument("--max-stale-seconds", type=int, default=300, help="Reject quotes older than this. Default: 300")
    parser.add_argument(
        "--force-exit-time", default="15:15",
        help="HH:MM IST exit time on the final allowed trading session. Default: 15:15",
    )
    parser.add_argument(
        "--disable-time-exit",
        action="store_true",
        help="Disable the final-session time exit; positions then exit only on target or stop-loss.",
    )
    parser.add_argument(
        "--max-open-days", type=int, default=None,
        help="Optional override for TRADE_HORIZON_DAYS; entry session counts as day 1.",
    )
    parser.add_argument(
        "--skip-pending-entry",
        action="store_true",
        help="Do not retry today's PLANNED entries (including CALL reclaim watches).",
    )
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date) if args.trade_date else None
    from src.common.config import get_trade_horizon_days
    max_open_days = args.max_open_days if args.max_open_days is not None else get_trade_horizon_days()
    pending_entry = None
    if not args.skip_pending_entry:
        pending_entry = enter_due_paper_trades(
            trade_date=trade_date or _default_trade_date(),
            symbol=args.underlying.upper(),
            model_version=args.model_version,
            slippage_pct=0.0,
            max_stale_seconds=args.max_stale_seconds,
        )
    result = monitor_open_paper_trades(
        trade_date=trade_date,
        symbol=args.underlying.upper(),
        model_version=args.model_version,
        slippage_pct=args.slippage_pct,
        max_stale_seconds=args.max_stale_seconds,
        force_exit_time=None if args.disable_time_exit else _parse_time(args.force_exit_time),
        max_open_days=max_open_days,
    )
    print({
        "trade_date": trade_date.isoformat() if trade_date else "ALL_OPEN",
        "underlying": args.underlying.upper(),
        "model_version": args.model_version,
        "pending_entry_retry": pending_entry,
        **result,
    })


if __name__ == "__main__":
    main()
