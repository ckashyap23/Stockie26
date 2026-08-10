"""
Backfill 5-minute intraday OptionSnapshot rows for every option instrument
that was selected by NiftyOptionSelection in a given date range.

Workflow
--------
1. Query NiftyOptionSelection for all signal_dates in [--start, --end]
   where primary_buy_symbol IS NOT NULL.
2. For each selected instrument resolve the replay trade_dates
   (entry date + hold days from TradingCalendar up to TRADE_HORIZON_DAYS).
3. Fetch full-day 5-minute candles from Kite and upsert into OptionSnapshot
   with data_source = KITE_HISTORICAL_5M_CLOSE_PROXY.

After this runs, re-run pipeline_backtest_pnl.py for the same date range.
The PnL simulation will then find M5_0930 as entry and use the cascade
stop-loss / target logic over intraday snapshots instead of a single EOD price.

Usage
-----
# Backfill all selected instruments for July 2026:
python scripts/backfill_NIFTY/backfill_option_5m_by_symbol.py \\
    --start 2026-07-01 --end 2026-07-31

# Filter to a single symbol (useful for spot-fixes):
python scripts/backfill_NIFTY/backfill_option_5m_by_symbol.py \\
    --start 2026-07-01 --end 2026-07-31 --symbol NIFTY26AUG24300PE

# Dry-run to preview what would be fetched:
python scripts/backfill_NIFTY/backfill_option_5m_by_symbol.py \\
    --start 2026-07-01 --end 2026-07-31 --dry-run

# Force re-fetch even when M5 data already exists:
python scripts/backfill_NIFTY/backfill_option_5m_by_symbol.py \\
    --start 2026-07-01 --end 2026-07-31 --force
"""

from __future__ import annotations

import argparse
import sys
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings, get_trade_horizon_days
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.kite_client import KiteClient

# Re-use helpers from the bulk backfill script
from scripts.backfill_NIFTY.backfill_NIFTYoptions_from_historical import (
    build_snapshot_row_from_candle,
    fetch_5m_candles_range,
    snapshot_label_for_candle_time,
    SNAPSHOT_LABEL_MODE_5M,
)

SLEEP_BETWEEN_REQUESTS = 0.35


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_selected_instruments(
    db,
    start_date: date,
    end_date: date,
    model_version: str,
    symbol_filter: str | None,
) -> list[dict[str, Any]]:
    """
    Return one row per (signal_date, instrument) from NiftyOptionSelection
    where primary_buy_symbol IS NOT NULL in the given range.
    """
    sql = """
        SELECT
            o.trade_date          AS signal_date,
            o.primary_buy_symbol  AS tradingsymbol,
            o.primary_buy_token   AS instrument_token,
            COALESCE(tc_next.next_trade_date, p.next_trade_date) AS entry_trade_date
        FROM "NiftyOptionSelection" o
        LEFT JOIN "NiftyPrediction" p
          ON p.symbol = o.symbol
         AND p.signal_date = o.trade_date
         AND p.model_version = o.model_version
        LEFT JOIN LATERAL (
            SELECT MIN(tc.calendar_date) AS next_trade_date
            FROM "TradingCalendar" tc
            WHERE tc.exchange = 'NSE'
              AND tc.calendar_date > o.trade_date
              AND tc.is_trading_day = true
        ) tc_next ON true
        WHERE o.model_version = %s
          AND o.trade_date >= %s
          AND o.trade_date <= %s
          AND o.primary_buy_symbol IS NOT NULL
          AND o.primary_buy_token  IS NOT NULL
    """
    params: list[Any] = [model_version, start_date, end_date]
    if symbol_filter:
        sql += " AND o.primary_buy_symbol = %s"
        params.append(symbol_filter)
    sql += " ORDER BY o.trade_date"

    with db.conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Coerce dates
    for r in rows:
        for field in ("signal_date", "entry_trade_date"):
            v = r.get(field)
            if v is not None and hasattr(v, "date"):
                r[field] = v.date()
            elif isinstance(v, str):
                r[field] = date.fromisoformat(v)
        r["instrument_token"] = int(r["instrument_token"])

    return rows


def _resolve_instrument_id(db, tradingsymbol: str, instrument_token: int) -> int | None:
    """Return OptionInstrument.id for the given tradingsymbol / token."""
    with db.conn.cursor() as cur:
        cur.execute(
            'SELECT id FROM "OptionInstrument" WHERE instrument_token = %s LIMIT 1',
            (instrument_token,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _hold_dates(db, entry_trade_date: date, n: int) -> list[date]:
    """Return up to n trading dates starting from entry_trade_date."""
    with db.conn.cursor() as cur:
        cur.execute(
            """
            SELECT calendar_date FROM "TradingCalendar"
            WHERE exchange = 'NSE' AND is_trading_day = true
              AND calendar_date >= %s
            ORDER BY calendar_date LIMIT %s
            """,
            (entry_trade_date, n),
        )
        dates = [r[0] for r in cur.fetchall()]
    return dates or [entry_trade_date]


def _existing_m5_dates(db, instrument_id: int, trade_dates: list[date]) -> set[date]:
    """Return the subset of trade_dates that already have at least one M5 snapshot."""
    with db.conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT trade_date FROM "OptionSnapshot"
            WHERE option_instrument_id = %s
              AND trade_date = ANY(%s)
              AND snapshot_label LIKE 'M5_%%'
            """,
            (instrument_id, trade_dates),
        )
        return {r[0] for r in cur.fetchall()}


def _get_underlying_price(db, underlying: str, trade_date: date) -> float | None:
    """Return EOD close_price for the underlying on trade_date (best available proxy)."""
    with db.conn.cursor() as cur:
        cur.execute(
            'SELECT close_price FROM "UnderlyingSnapshot"'
            " WHERE underlying = %s AND trade_date = %s LIMIT 1",
            (underlying.upper(), trade_date),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _resolve_underlying(db, instrument_token: int) -> str:
    with db.conn.cursor() as cur:
        cur.execute(
            'SELECT underlying FROM "OptionInstrument" WHERE instrument_token = %s LIMIT 1',
            (instrument_token,),
        )
        row = cur.fetchone()
    return row[0] if row else "NIFTY"


# ---------------------------------------------------------------------------
# Core per-instrument backfill
# ---------------------------------------------------------------------------

def backfill_instrument_5m(
    instrument_token: int,
    instrument_id: int,
    tradingsymbol: str,
    underlying: str,
    trade_dates: list[date],
    kite_client: KiteClient,
    db,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    if not trade_dates:
        return 0

    # Skip dates that already have M5 coverage unless --force
    if not force:
        covered = _existing_m5_dates(db, instrument_id, trade_dates)
        trade_dates = [d for d in trade_dates if d not in covered]
        if not trade_dates:
            print(f"  SKIP {tradingsymbol} — M5 data already present for all hold dates.")
            return 0

    from_date = min(trade_dates)
    to_date = max(trade_dates)
    trade_date_set = set(trade_dates)

    if dry_run:
        print(f"  [DRY-RUN] {tradingsymbol}: would fetch {from_date}→{to_date} "
              f"({len(trade_dates)} date(s)) — skipping Kite call.")
        return len(trade_dates) * 75  # ~75 candles per trading day as estimate

    try:
        candles = fetch_5m_candles_range(
            kite_client=kite_client,
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            include_oi=True,
        )
    except Exception as exc:
        print(f"  [WARN] Kite error for {tradingsymbol}: {exc}")
        return 0

    time_module.sleep(SLEEP_BETWEEN_REQUESTS)

    if not candles:
        print(f"  [WARN] No candles returned for {tradingsymbol} {from_date}→{to_date}.")
        return 0

    # Pre-fetch per-date underlying EOD close (used as underlying_price context on each row)
    underlying_by_date: dict[date, float | None] = {}
    for d in trade_date_set:
        underlying_by_date[d] = _get_underlying_price(db, underlying, d)

    # Build instrument dict expected by build_snapshot_row_from_candle
    instr = {"id": instrument_id, "instrument_token": instrument_token,
              "underlying": underlying, "tradingsymbol": tradingsymbol}

    rows: list[dict[str, Any]] = []
    for candle in candles:
        candle_dt = candle.get("date")
        if not isinstance(candle_dt, datetime):
            continue
        candle_dt = candle_dt.replace(tzinfo=None)
        trade_date = candle_dt.date()
        if trade_date not in trade_date_set:
            continue

        snapshot_label = snapshot_label_for_candle_time(candle_dt.time(), SNAPSHOT_LABEL_MODE_5M)
        if snapshot_label is None:
            continue

        rows.append(build_snapshot_row_from_candle(
            instrument=instr,
            candle=candle,
            trade_date=trade_date,
            snapshot_label=snapshot_label,
            snapshot_time=candle_dt,
            underlying_price=underlying_by_date.get(trade_date) or 0.0,
        ))

    if not rows:
        print(f"  [WARN] No rows built for {tradingsymbol} after label filtering.")
        return 0

    upserted = db.bulk_insert_option_snapshots(rows)
    return upserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill 5-minute intraday OptionSnapshot data for every option "
            "selected by NiftyOptionSelection in a date range. Run this before "
            "pipeline_backtest_pnl.py to enable accurate intraday exit simulation."
        )
    )
    parser.add_argument("--start", required=True, metavar="YYYY-MM-DD",
                        help="First signal_date to include (inclusive).")
    parser.add_argument("--end", required=True, metavar="YYYY-MM-DD",
                        help="Last signal_date to include (inclusive).")
    parser.add_argument("--symbol", default=None, metavar="TRADINGSYMBOL",
                        help="Optional: restrict to a single option tradingsymbol.")
    parser.add_argument("--model-version", default="cascade_v1",
                        help="NiftyOptionSelection model_version filter. Default: cascade_v1")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview fetches without writing to the DB.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch and overwrite even when M5 data already exists.")

    args = parser.parse_args()
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    n_hold = get_trade_horizon_days()

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()

    kite_client = KiteClient(settings)
    kite_client.authenticate()

    try:
        print(f"Loading NiftyOptionSelection rows {start_date} → {end_date} ...")
        selections = _load_selected_instruments(
            db, start_date, end_date, args.model_version, args.symbol
        )
        print(f"  {len(selections)} selection(s) found.")

        if not selections:
            print("Nothing to backfill.")
            return

        total_upserted = 0
        for idx, sel in enumerate(selections, 1):
            tradingsymbol = sel["tradingsymbol"]
            instrument_token = sel["instrument_token"]
            entry_trade_date = sel["entry_trade_date"]

            instrument_id = _resolve_instrument_id(db, tradingsymbol, instrument_token)
            if instrument_id is None:
                print(f"  [{idx}/{len(selections)}] SKIP {tradingsymbol} — not in OptionInstrument.")
                continue

            underlying = _resolve_underlying(db, instrument_token)
            trade_dates = _hold_dates(db, entry_trade_date, n_hold)

            print(
                f"  [{idx}/{len(selections)}] {sel['signal_date']} | {tradingsymbol} | "
                f"entry={entry_trade_date} | hold_dates={trade_dates}"
            )
            n = backfill_instrument_5m(
                instrument_token=instrument_token,
                instrument_id=instrument_id,
                tradingsymbol=tradingsymbol,
                underlying=underlying,
                trade_dates=trade_dates,
                kite_client=kite_client,
                db=db,
                dry_run=args.dry_run,
                force=args.force,
            )
            total_upserted += n
            if not args.dry_run and n > 0:
                print(f"    Upserted {n} snapshot row(s).")

        verb = "would upsert" if args.dry_run else "upserted"
        print(f"\nDone. Total rows {verb}: {total_upserted}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
