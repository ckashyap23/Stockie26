"""Refresh D macro inputs before the D+1 NIFTY open and update signal features."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
import sys

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.macro_factor_loader import run_macro_factor_load
from scripts.Common.calculate_macro_features import run_calculate_macro_features
from scripts.Common.schedule_guard import skip_if_weekend


def _previous_nse_session(as_of_date: date) -> date:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(calendar_date) FROM "TradingCalendar"
                WHERE exchange = 'NSE'
                  AND is_trading_day = true
                  AND calendar_date < %s
                """,
                (as_of_date,),
            )
            row = cur.fetchone()
    finally:
        db.close()
    if not row or row[0] is None:
        raise RuntimeError("TradingCalendar has no prior NSE session")
    return row[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load point-in-time macro inputs and update SignalFeatureDaily."
    )
    parser.add_argument("--start", help="Signal start date YYYY-MM-DD")
    parser.add_argument("--end", help="Signal end date YYYY-MM-DD")
    parser.add_argument(
        "--fii-dii-csv",
        type=Path,
        help="Optional NSE archive CSV for historical FII/DII backfills.",
    )
    args = parser.parse_args()

    if not args.start and skip_if_weekend("Macro refresh"):
        return

    if bool(args.start) != bool(args.end):
        raise ValueError("--start and --end must be provided together")
    if args.start:
        start_date = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end)
    else:
        start_date = end_date = _previous_nse_session(date.today())
    if start_date > end_date:
        raise ValueError("start date must be <= end date")

    load_summary = run_macro_factor_load(
        start_date=start_date,
        end_date=end_date,
        fii_dii_csv=args.fii_dii_csv,
    )
    feature_summary = run_calculate_macro_features(start_date, end_date)
    print({"load": load_summary, "features": feature_summary})


if __name__ == "__main__":
    main()
