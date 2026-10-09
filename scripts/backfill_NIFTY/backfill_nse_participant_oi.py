"""Backfill official NSE participant-wise FII index-futures open interest."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from scripts.Common.calculate_macro_features import run_calculate_macro_features
from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.macro_factor_loader import (
    fetch_nse_participant_oi_dates,
    upsert_macro_factors,
)


def _trading_dates(start_date: date, end_date: date) -> list[date]:
    db = get_database_client(get_settings())
    if getattr(db, "db_kind", "") != "postgres":
        raise RuntimeError("Participant OI backfill supports Supabase/Postgres only")
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT session_date
                FROM (
                    SELECT calendar_date AS session_date
                    FROM "TradingCalendar"
                    WHERE exchange = 'NSE'
                      AND is_trading_day = true
                    UNION
                    SELECT signal_date AS session_date
                    FROM "SignalFeatureDaily"
                    WHERE UPPER(symbol) = 'NIFTY'
                ) AS sessions
                WHERE session_date BETWEEN %s AND %s
                ORDER BY session_date
                """,
                (start_date, end_date),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        db.close()


def run_backfill(start_date: date, end_date: date, max_workers: int = 4) -> dict:
    if start_date > end_date:
        raise ValueError("start date must be <= end date")
    trading_dates = _trading_dates(start_date, end_date)
    if not trading_dates:
        raise RuntimeError("No NSE calendar or NIFTY signal dates in the requested range")

    rows, missing = fetch_nse_participant_oi_dates(
        trading_dates,
        max_workers=max_workers,
    )
    if missing:
        raise RuntimeError(
            "NSE participant OI archives are missing for trading dates: "
            + ", ".join(value.isoformat() for value in missing)
        )
    rows["participant_oi_source"] = "nse_archive"
    raw_count = upsert_macro_factors(rows)
    feature_summary = run_calculate_macro_features(start_date, end_date)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "trading_dates": len(trading_dates),
        "raw_rows_upserted": raw_count,
        "feature_rows_updated": feature_summary["rows_updated"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill NSE participant-wise FII index-futures OI features."
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()
    print(
        run_backfill(
            date.fromisoformat(args.start),
            date.fromisoformat(args.end),
            max_workers=args.max_workers,
        )
    )


if __name__ == "__main__":
    main()
