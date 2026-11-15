# scripts/backfill_NIFTY/backfill_india_vix.py
"""
Backfill daily India VIX into the Supabase macro table "MacroFactorDaily".

India VIX is NSE's 30-day forward volatility index (NSE:INDIA VIX, segment
INDICES). It is strongly negatively correlated with NIFTY and is a leading /
coincident signal for down-days, so it is stored as a daily macro feature for
underlying direction-prediction research.

MacroFactorDaily mirrors the legacy Azure-SQL placeholder (migration 001) and
is the shared home for future macro/external signals (GIFT Nifty, US indices,
crude, USD/INR, bond yields, event flags). This script only populates the
india_vix column; other columns are left NULL for future backfills.

Writes:
  - public."MacroFactorDaily"  (Supabase / postgres)

Usage:
  python scripts/backfill_NIFTY/backfill_india_vix.py                  # 2025-01-01 -> today
  python scripts/backfill_NIFTY/backfill_india_vix.py --start 2025-01-01 --end 2026-06-18
"""

from technical_analysis_service.runtime import SERVICE_ROOT
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv

project_root = SERVICE_ROOT

from technical_analysis_service.common.config import get_settings
from technical_analysis_service.data_manager.db.client_factory import get_database_client
from technical_analysis_service.data_manager.kite_client import KiteClient

load_dotenv()

BACKFILL_START = date(2025, 1, 1)
VIX_TRADINGSYMBOL = "INDIA VIX"
VIX_EXCHANGE = "NSE"

# Supabase/postgres port of the legacy dbo.MacroFactorDaily placeholder
# (src/data_manager/db/migrations/001_create_trading_system_tables.sql).
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "MacroFactorDaily" (
    factor_date            date NOT NULL,
    india_vix              double precision,
    gift_nifty_return_pct  double precision,
    dow_return_pct         double precision,
    nasdaq_return_pct      double precision,
    sp500_return_pct       double precision,
    crude_return_pct       double precision,
    usd_inr_return_pct     double precision,
    bond_yield_change      double precision,
    event_flag             boolean NOT NULL DEFAULT false,
    event_type             varchar(100),
    source_json            jsonb,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz,
    CONSTRAINT pk_macro_factor_daily PRIMARY KEY (factor_date)
);
"""

# Upsert only the india_vix column; preserve any other macro columns already set.
UPSERT_SQL = """
INSERT INTO "MacroFactorDaily" (factor_date, india_vix, updated_at)
VALUES %s
ON CONFLICT (factor_date) DO UPDATE SET
    india_vix  = EXCLUDED.india_vix,
    updated_at = now()
"""


def _resolve_vix_token(kite_client: KiteClient) -> int:
    """Find the India VIX instrument_token from the NSE instruments dump."""
    instruments = kite_client.kite.instruments(VIX_EXCHANGE)
    for ki in instruments:
        if ki.get("tradingsymbol", "").strip().upper() == VIX_TRADINGSYMBOL:
            return int(ki["instrument_token"])
    raise RuntimeError(
        f"Could not resolve '{VIX_TRADINGSYMBOL}' token on {VIX_EXCHANGE}. "
        "Check the instrument name in the Kite instruments dump."
    )


def _fetch_daily_vix(
    kite_client: KiteClient,
    token: int,
    start_date: date,
    end_date: date,
) -> List[Tuple[date, datetime, float | None]]:
    """Return (trade_date, loaded_at, vix_close) rows. VIX close is the
    convention used by the legacy india_vix column."""
    loaded_at = datetime.now()
    rows: List[Tuple[date, datetime, float | None]] = []
    candles = kite_client.kite.historical_data(
        token,
        datetime.combine(start_date, dtime(9, 15)),
        datetime.combine(end_date, dtime(15, 30)),
        interval="day", continuous=False, oi=False,
    )
    for c in candles:
        trade_dt = c["date"].replace(tzinfo=None).date()
        if not (start_date <= trade_dt <= end_date):
            continue
        close = float(c["close"]) if c.get("close") is not None else None
        rows.append((trade_dt, loaded_at, close))
    return rows


def run_backfill_india_vix(start_date: date, end_date: date) -> dict:
    settings = get_settings()

    db = get_database_client(settings)
    if getattr(db, "db_kind", "") != "postgres":
        raise RuntimeError(
            "backfill_india_vix currently supports the Supabase/postgres provider only. "
            "Set DATABASE_PROVIDER=supabase."
        )

    kite_client = KiteClient(settings)
    kite_client.authenticate()
    token = _resolve_vix_token(kite_client)
    print(f"Resolved {VIX_TRADINGSYMBOL} token={token}")

    print(f"Fetching daily India VIX {start_date} -> {end_date} ...")
    rows = _fetch_daily_vix(kite_client, token, start_date, end_date)
    print(f"Fetched {len(rows)} daily VIX rows")

    # Map to (factor_date, india_vix, updated_at) for the macro upsert.
    upsert_rows = [(r[0], r[2], r[1]) for r in rows]

    db.connect()
    try:
        from psycopg2.extras import execute_values

        with db.conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            if upsert_rows:
                execute_values(cur, UPSERT_SQL, upsert_rows)
        db.conn.commit()
    finally:
        db.close()

    print(f"Upserted {len(upsert_rows)} india_vix rows into MacroFactorDaily.")
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "rows": len(upsert_rows),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Backfill daily India VIX into Supabase MacroFactorDaily")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD. Default: 2025-01-01")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD. Default: today")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start) if args.start else BACKFILL_START
    end_date = date.fromisoformat(args.end) if args.end else date.today()

    run_backfill_india_vix(start_date, end_date)


if __name__ == "__main__":
    main()
