"""Backfill GiftNiftySnapshot with the 9:15-9:20 AM IST 5-minute candle
for every NIFTY trading day from a given start date.

The close_920 value (GIFT NIFTY price at 9:20 AM IST) is the primary output:
it captures the pre-open level of GIFT NIFTY at the moment India's cash market
opens, giving a directional read before any domestic signal is generated.

Usage:
    python scripts/backfill_NIFTY/backfill_gift_nifty.py
    python scripts/backfill_NIFTY/backfill_gift_nifty.py --start 2024-01-01 --end 2026-07-21
"""
from __future__ import annotations

import sys
import argparse
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.supabase_client import SupabaseDatabaseClient
from src.data_manager.kite_client import KiteClient

GIFT_NIFTY_TOKEN = 291849          # instrument_token for 'GIFT NIFTY' on NSEIX
CANDLE_TIME_IST  = dtime(9, 15)    # the 5-min candle that opens at 9:15 AM IST
IST = ZoneInfo("Asia/Kolkata")
CHUNK_DAYS = 60                    # Kite historical API cap per request


def _chunks(start: date, end: date, size: int):
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=size - 1), end)
        cur += timedelta(days=size)


def run_backfill_gift_nifty(
    start_date: date,
    end_date: date,
) -> dict:
    settings = get_settings()
    kc = KiteClient(settings)
    kc.authenticate()

    db = SupabaseDatabaseClient(settings)
    db.connect()

    fetched_total = 0
    rows: list[tuple] = []
    now = datetime.now(IST).replace(tzinfo=None)

    print(f"Fetching GIFT NIFTY 9:15 AM candles {start_date} to {end_date} ...")
    for chunk_start, chunk_end in _chunks(start_date, end_date, CHUNK_DAYS):
        print(f"  Chunk: {chunk_start} to {chunk_end}")
        try:
            candles = kc.kite.historical_data(
                GIFT_NIFTY_TOKEN,
                datetime.combine(chunk_start, dtime(9, 10)),   # fetch from 9:10 for safety
                datetime.combine(chunk_end,   dtime(9, 21)),   # just past 9:20
                interval="5minute", continuous=False, oi=False,
            )
        except Exception as exc:
            print(f"  [WARN] Kite fetch failed: {exc}")
            continue

        for c in candles:
            candle_time = c["date"]
            if hasattr(candle_time, "tzinfo") and candle_time.tzinfo is not None:
                candle_time = candle_time.astimezone(IST).replace(tzinfo=None)
            if candle_time.time() != CANDLE_TIME_IST:
                continue
            trade_dt = candle_time.date()
            if trade_dt < start_date or trade_dt > end_date:
                continue
            rows.append((
                trade_dt,
                float(c["open"]),
                float(c["high"]),
                float(c["low"]),
                float(c["close"]),
                now,
            ))
            fetched_total += 1

    print(f"\nUploading {fetched_total} rows to GiftNiftySnapshot ...")
    upserted = db.upsert_gift_nifty_snapshots(rows)
    db.close()
    print(f"Done — {upserted} rows upserted.")
    return {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "fetched": fetched_total, "upserted": upserted}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill GiftNiftySnapshot (9:15-9:20 AM IST 5m candle) from Kite."
    )
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD (default: 2024-01-01)")
    parser.add_argument("--end",   default=None,         help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end) if args.end else date.today()
    run_backfill_gift_nifty(start, end)


if __name__ == "__main__":
    main()
