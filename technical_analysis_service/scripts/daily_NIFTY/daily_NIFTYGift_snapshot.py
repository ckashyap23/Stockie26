"""
daily_NIFTYGift_snapshot.py — capture GIFT NIFTY 5m snapshots.

Three scheduled modes (run at the times shown):
  --mode pre-open (9:11 AM IST): saves gift_0910 = close of the 9:05-9:10 AM candle
                                 (last completed price before the 09:12 decision)
  --mode open   (9:20 AM IST) : saves close_920  = close of the 9:15-9:20 AM candle
  --mode close  (3:15 PM IST) : saves gift_1515  = close of the 3:10-3:15 PM candle

gift_1515 is used the *next morning* as the D-1 reference in:
    gift_gap_pct(D) = gift_920(D) / gift_1515(D-1) - 1
and, for the pre-open decision:
    gift_gap_0910(D) = gift_0910(D) / gift_1515(D-1) - 1

Backfill mode:
  --mode backfill --start YYYY-MM-DD --end YYYY-MM-DD
  Fetches 5m candles from Kite for the date range, extracts all three snapshots
  (gift_0910, close_920, gift_1515), and upserts them into GiftNiftySnapshot.
  Existing non-NULL values are never overwritten with NULL.

Usage:
    python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode pre-open
    python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode open
    python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode close
    python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode backfill --start 2024-01-01 --end 2026-07-22
"""
from __future__ import annotations

from technical_analysis_service.runtime import SERVICE_ROOT, load_environment

import argparse
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

project_root = SERVICE_ROOT
load_environment()

from technical_analysis_service.common.config import get_settings
from technical_analysis_service.data_manager.db.supabase_client import SupabaseDatabaseClient
from technical_analysis_service.data_manager.kite_client import KiteClient
from technical_analysis_service.scripts.Common.schedule_guard import skip_if_weekend

GIFT_NIFTY_TOKEN = 291849
IST = ZoneInfo("Asia/Kolkata")
PRE_OPEN_CANDLE_TIME = dtime(9, 5)  # candle that closes at 9:10 AM → gift_0910
PRE_OPEN_READY_TIME = dtime(9, 10)  # earliest time the 9:05 candle is complete
OPEN_CANDLE_TIME  = dtime(9, 15)   # candle that closes at 9:20 AM → gift_920
CLOSE_CANDLE_TIME = dtime(15, 10)  # candle that closes at 3:15 PM → gift_1515
CHUNK_DAYS = 60


def _candle_at(candles: list[dict], target_time: dtime, trade_date: date) -> dict | None:
    for c in candles:
        ct = c["date"]
        if hasattr(ct, "tzinfo") and ct.tzinfo:
            ct = ct.astimezone(IST).replace(tzinfo=None)
        if ct.time() == target_time and ct.date() == trade_date:
            return c
    return None


def _fetch_candles(kc: KiteClient, from_dt: datetime, to_dt: datetime) -> list[dict]:
    try:
        return kc.kite.historical_data(
            GIFT_NIFTY_TOKEN, from_dt, to_dt,
            interval="5minute", continuous=False, oi=False,
        )
    except Exception as exc:
        print(f"  [WARN] Kite fetch failed: {exc}")
        return []


# ── live modes ────────────────────────────────────────────────────────────────

def run_pre_open(db: SupabaseDatabaseClient, kc: KiteClient, trade_date: date) -> dict:
    """Fetch the 9:05 AM candle and save gift_0910 (its close, i.e. price at 9:10)."""
    now = datetime.now(IST).replace(tzinfo=None)
    if now.date() == trade_date and now.time() < PRE_OPEN_READY_TIME:
        print(f"  [WARN] 9:05 candle is not complete before {PRE_OPEN_READY_TIME}; not saving gift_0910.")
        return {"mode": "pre-open", "trade_date": str(trade_date), "status": "too_early"}
    candles = _fetch_candles(
        kc,
        datetime.combine(trade_date, dtime(9, 0)),
        datetime.combine(trade_date, dtime(9, 11)),
    )
    candle = _candle_at(candles, PRE_OPEN_CANDLE_TIME, trade_date)
    if not candle:
        print(f"  [WARN] GIFT NIFTY 9:05 candle not found for {trade_date}")
        return {"mode": "pre-open", "trade_date": str(trade_date), "status": "missing"}
    db.upsert_gift_nifty_0910([(trade_date, float(candle["close"]), now)])
    print(f"  gift_0910 saved for {trade_date}: close={candle['close']}")
    return {"mode": "pre-open", "trade_date": str(trade_date),
            "status": "ok", "gift_0910": float(candle["close"])}


def run_open(db: SupabaseDatabaseClient, kc: KiteClient, trade_date: date) -> dict:
    """Fetch 9:15 AM candle and save gift_920 (close_920) for trade_date."""
    now = datetime.now(IST).replace(tzinfo=None)
    candles = _fetch_candles(
        kc,
        datetime.combine(trade_date, dtime(9, 10)),
        datetime.combine(trade_date, dtime(9, 21)),
    )
    candle = _candle_at(candles, OPEN_CANDLE_TIME, trade_date)
    if not candle:
        print(f"  [WARN] GIFT NIFTY 9:15 candle not found for {trade_date}")
        return {"mode": "open", "trade_date": str(trade_date), "status": "missing"}
    db.upsert_gift_nifty_snapshots([(
        trade_date,
        float(candle["open"]), float(candle["high"]),
        float(candle["low"]),  float(candle["close"]),
        now,
    )])
    print(f"  gift_920 saved for {trade_date}: close={candle['close']}")
    return {"mode": "open", "trade_date": str(trade_date),
            "status": "ok", "gift_920": float(candle["close"])}


def run_close(db: SupabaseDatabaseClient, kc: KiteClient, trade_date: date) -> dict:
    """Fetch 3:10 PM candle and save gift_1515 for trade_date."""
    now = datetime.now(IST).replace(tzinfo=None)
    candles = _fetch_candles(
        kc,
        datetime.combine(trade_date, dtime(15, 8)),
        datetime.combine(trade_date, dtime(15, 16)),
    )
    candle = _candle_at(candles, CLOSE_CANDLE_TIME, trade_date)
    if not candle:
        print(f"  [WARN] GIFT NIFTY 3:10 PM candle not found for {trade_date}")
        return {"mode": "close", "trade_date": str(trade_date), "status": "missing"}
    db.upsert_gift_nifty_1515([(trade_date, float(candle["close"]), now)])
    print(f"  gift_1515 saved for {trade_date}: close={candle['close']}")
    return {"mode": "close", "trade_date": str(trade_date),
            "status": "ok", "gift_1515": float(candle["close"])}


# ── backfill mode ─────────────────────────────────────────────────────────────

def run_backfill(
    db: SupabaseDatabaseClient,
    kc: KiteClient,
    start_date: date,
    end_date: date,
) -> dict:
    """Fetch full 5m candle history, extract both snapshots, upsert all rows."""
    now = datetime.now(IST).replace(tzinfo=None)
    full_rows: list[tuple] = []
    pre_open_rows: list[tuple] = []
    cur = start_date
    total_fetched = 0

    print(f"Backfill: GIFT NIFTY snapshots {start_date} to {end_date} ...")
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), end_date)
        print(f"  Chunk: {cur} to {chunk_end}")
        candles = _fetch_candles(
            kc,
            datetime.combine(cur, dtime(9, 0)),
            datetime.combine(chunk_end, dtime(15, 16)),
        )
        total_fetched += len(candles)

        # Index candles by (date, time)
        by_dt: dict[tuple, dict] = {}
        for c in candles:
            ct = c["date"]
            if hasattr(ct, "tzinfo") and ct.tzinfo:
                ct = ct.astimezone(IST).replace(tzinfo=None)
            by_dt[(ct.date(), ct.time())] = c

        # Collect rows for dates in this chunk that have at least one snapshot
        d = cur
        while d <= chunk_end:
            c_open  = by_dt.get((d, OPEN_CANDLE_TIME))
            c_close = by_dt.get((d, CLOSE_CANDLE_TIME))
            c_pre   = by_dt.get((d, PRE_OPEN_CANDLE_TIME))
            if c_pre:
                pre_open_rows.append((d, float(c_pre["close"]), now))
            if c_open or c_close:
                full_rows.append((
                    d,
                    float(c_open["open"])  if c_open  else None,
                    float(c_open["high"])  if c_open  else None,
                    float(c_open["low"])   if c_open  else None,
                    float(c_open["close"]) if c_open  else None,
                    float(c_close["close"]) if c_close else None,
                    now,
                ))
            d += timedelta(days=1)
        cur = chunk_end + timedelta(days=1)

    print(f"Fetched {total_fetched} raw candles; {len(full_rows)} dates have an open/close "
          f"snapshot and {len(pre_open_rows)} have a 9:10 snapshot.")
    if full_rows:
        upserted = db.upsert_gift_nifty_full(full_rows)
        print(f"Upserted {upserted} rows into GiftNiftySnapshot.")
    if pre_open_rows:
        upserted_pre = db.upsert_gift_nifty_0910(pre_open_rows)
        print(f"Upserted {upserted_pre} gift_0910 values into GiftNiftySnapshot.")
    return {
        "mode": "backfill", "start": str(start_date), "end": str(end_date),
        "rows": len(full_rows), "gift_0910_rows": len(pre_open_rows),
        "gift_1515_rows": sum(1 for r in full_rows if r[5] is not None),
    }


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture GIFT NIFTY 5m snapshots (gift_0910, gift_920 and/or gift_1515)."
    )
    parser.add_argument("--underlying", default="NIFTY",
                        help="Underlying symbol (reserved for future multi-underlying support).")
    parser.add_argument("--mode", choices=["pre-open", "open", "close", "backfill"], required=True,
                        help="pre-open=9:11 AM (gift_0910), open=9:20 AM (gift_920), "
                             "close=3:15 PM (gift_1515), backfill=all three.")
    parser.add_argument("--start", default="2024-01-01",
                        help="Backfill start date (default: 2024-01-01).")
    parser.add_argument("--end", default=None,
                        help="Backfill end date (default: today).")
    args = parser.parse_args()

    if args.mode != "backfill" and skip_if_weekend(f"GIFT {args.mode}"):
        return

    settings = get_settings()
    db = SupabaseDatabaseClient(settings)
    db.connect()

    # Apply migrations 033 and 048 (idempotent)
    migrations_dir = SERVICE_ROOT / "data_manager" / "db" / "migrations"
    for mig_name in ("033_add_gift_nifty_1515.sql", "048_add_gift_nifty_0910.sql"):
        with db.conn.cursor() as cur:
            cur.execute((migrations_dir / mig_name).read_text(encoding="utf-8"))
    db.conn.commit()

    try:
        kc = KiteClient(settings)
        kc.authenticate()

        if args.mode == "pre-open":
            result = run_pre_open(db, kc, date.today())
        elif args.mode == "open":
            result = run_open(db, kc, date.today())
        elif args.mode == "close":
            result = run_close(db, kc, date.today())
        else:
            start = date.fromisoformat(args.start)
            end   = date.fromisoformat(args.end) if args.end else date.today()
            result = run_backfill(db, kc, start, end)
    finally:
        db.close()

    print(result)


if __name__ == "__main__":
    main()
