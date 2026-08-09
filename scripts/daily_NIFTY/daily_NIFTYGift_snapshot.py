"""
daily_NIFTYGift_snapshot.py — capture GIFT NIFTY 5m snapshots.

Two scheduled modes (run at the times shown):
  --mode open   (9:20 AM IST) : saves close_920  = close of the 9:15-9:20 AM candle
  --mode close  (3:15 PM IST) : saves gift_1515  = close of the 3:10-3:15 PM candle

gift_1515 is used the *next morning* as the D-1 reference in:
    gift_gap_pct(D) = gift_920(D) / gift_1515(D-1) - 1

Backfill mode:
  --mode backfill --start YYYY-MM-DD --end YYYY-MM-DD
  Fetches 5m candles from Kite for the date range, extracts both snapshots,
  and upserts all rows into GiftNiftySnapshot in one pass.

Usage:
    python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode open
    python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode close
    python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode backfill --start 2024-01-01 --end 2026-07-22
"""
from __future__ import annotations

import argparse
import sys
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

GIFT_NIFTY_TOKEN = 291849
IST = ZoneInfo("Asia/Kolkata")
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
    cur = start_date
    total_fetched = 0

    print(f"Backfill: GIFT NIFTY snapshots {start_date} to {end_date} ...")
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), end_date)
        print(f"  Chunk: {cur} to {chunk_end}")
        candles = _fetch_candles(
            kc,
            datetime.combine(cur, dtime(9, 10)),
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

    print(f"Fetched {total_fetched} raw candles; {len(full_rows)} dates have at least one snapshot.")
    if full_rows:
        upserted = db.upsert_gift_nifty_full(full_rows)
        print(f"Upserted {upserted} rows into GiftNiftySnapshot.")
    return {
        "mode": "backfill", "start": str(start_date), "end": str(end_date),
        "rows": len(full_rows),
    }


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture GIFT NIFTY 5m snapshots (gift_920 and/or gift_1515)."
    )
    parser.add_argument("--underlying", default="NIFTY",
                        help="Underlying symbol (reserved for future multi-underlying support).")
    parser.add_argument("--mode", choices=["open", "close", "backfill"], required=True,
                        help="open=9:20 AM (gift_920), close=3:15 PM (gift_1515), backfill=both.")
    parser.add_argument("--start", default="2024-01-01",
                        help="Backfill start date (default: 2024-01-01).")
    parser.add_argument("--end", default=None,
                        help="Backfill end date (default: today).")
    args = parser.parse_args()

    settings = get_settings()
    db = SupabaseDatabaseClient(settings)
    db.connect()

    # Apply migration 033 (idempotent)
    mig = project_root / "src" / "data_manager" / "db" / "migrations" / "033_add_gift_nifty_1515.sql"
    with db.conn.cursor() as cur:
        cur.execute(mig.read_text(encoding="utf-8"))
    db.conn.commit()

    try:
        kc = KiteClient(settings)
        kc.authenticate()

        if args.mode == "open":
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
