"""
Daily 9:20 AM IST open-gap script.

Runs at 9:20 AM IST each trading day (after the first 5-minute candle closes).
Performs three steps:

  1. Fetch NIFTY 9:15-9:20 AM 5m candle from Kite.
       - Save full OHLC to UnderlyingCandle5m (trade_date = D, candle_time = 09:15 IST).
  2. Fetch GIFT NIFTY 9:15-9:20 AM 5m candle from Kite.
       - Save close_920 (+ OHLC) to GiftNiftySnapshot (trade_date = D).
  3. Compute 7 open-gap features and upsert to SignalFeatureDaily for
       signal_date = D-1 (the signal row whose trade executes on D):

       nifty_gap_pct   = nifty_open_915(D)  / close_1515(D-1) - 1
       nifty_drift_pct = nifty_close_920(D) / nifty_open_915(D) - 1
       gift_gap_pct    = gift_nifty_920(D)  / close_1515(D-1) - 1
       gap_confirmed   = sign(nifty_gap_pct) == sign(gift_gap_pct)  and  gap != 0
       gap_fade        = gap != 0 and drift != 0 and sign(drift) != sign(gap)
       gap_open_atr    = nifty_gap_pct / (atr14 / close_1515)
       gift_gap_atr    = gift_gap_pct  / (atr14 / close_1515)

Backfill mode (--start / --end): reads candles from the already-populated
UnderlyingCandle5m and GiftNiftySnapshot tables instead of calling Kite.

Usage:
    python scripts/daily_NIFTY/daily_open_gap.py                   # live run (D = today)
    python scripts/daily_NIFTY/daily_open_gap.py --start 2024-01-01 --end 2026-07-21
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

IST = ZoneInfo("Asia/Kolkata")
GIFT_NIFTY_TOKEN = 291849
CANDLE_OPEN_TIME = dtime(9, 15)


# ── helpers ───────────────────────────────────────────────────────────────────

def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _compute_features(
    nifty_open: float,
    nifty_close: float,
    gift_close: float,
    prev_close: float,
    atr14: float | None,
    close_1515_d1: float | None,
) -> dict:
    gap   = nifty_open / prev_close - 1.0
    drift = nifty_close / nifty_open - 1.0 if nifty_open else 0.0
    gift  = gift_close / prev_close - 1.0

    gap_confirmed = _sign(gap) != 0 and _sign(gap) == _sign(gift)
    gap_fade = _sign(gap) != 0 and _sign(drift) != 0 and _sign(drift) != _sign(gap)

    atr_rel = (atr14 / close_1515_d1) if (atr14 and close_1515_d1) else None
    gap_atr  = round(gap  / atr_rel, 6) if atr_rel else None
    gift_atr = round(gift / atr_rel, 6) if atr_rel else None

    return {
        "nifty_gap_pct":   round(gap,   8),
        "nifty_drift_pct": round(drift, 8),
        "gift_gap_pct":    round(gift,  8),
        "gap_confirmed":   gap_confirmed,
        "gap_fade":        gap_fade,
        "gap_open_atr":    gap_atr,
        "gift_gap_atr":    gift_atr,
    }


def _filter_915(candles: list[dict], trade_date: date) -> dict | None:
    for c in candles:
        ct = c["date"]
        if hasattr(ct, "tzinfo") and ct.tzinfo is not None:
            ct = ct.astimezone(IST).replace(tzinfo=None)
        if ct.time() == CANDLE_OPEN_TIME and ct.date() == trade_date:
            return c
    return None


# ── live mode: fetch fresh from Kite ─────────────────────────────────────────

def _fetch_915_candle_kite(kc: KiteClient, token: int, trade_date: date) -> dict | None:
    try:
        candles = kc.kite.historical_data(
            token,
            datetime.combine(trade_date, dtime(9, 10)),
            datetime.combine(trade_date, dtime(9, 21)),
            interval="5minute", continuous=False, oi=False,
        )
    except Exception as exc:
        print(f"  [WARN] Kite fetch failed for token {token}: {exc}")
        return None
    return _filter_915(candles, trade_date)


def _resolve_nifty_token(kc: KiteClient) -> int | None:
    CANONICAL = {"NIFTY 50": "NIFTY", "NIFTY": "NIFTY"}
    try:
        instr = kc.kite.instruments("NSE")
        for i in instr:
            if i.get("tradingsymbol") in CANONICAL and i.get("segment") == "INDICES":
                return int(i["instrument_token"])
    except Exception as exc:
        print(f"  [WARN] Could not resolve NIFTY token: {exc}")
    return None


def run_live(db: SupabaseDatabaseClient, kc: KiteClient, trade_date: date) -> dict:
    """Fetch today's candles, save them, compute and upsert features."""
    now = datetime.now(IST).replace(tzinfo=None)

    # Step 1: NIFTY 9:15 candle
    nifty_token = _resolve_nifty_token(kc)
    nifty_candle = _fetch_915_candle_kite(kc, nifty_token, trade_date) if nifty_token else None
    if nifty_candle:
        ct = nifty_candle["date"]
        if hasattr(ct, "tzinfo") and ct.tzinfo:
            ct = ct.astimezone(IST).replace(tzinfo=None)
        db.upsert_underlying_candles_5m([(
            "NIFTY", trade_date, ct,
            float(nifty_candle["open"]), float(nifty_candle["high"]),
            float(nifty_candle["low"]),  float(nifty_candle["close"]),
            int(nifty_candle.get("volume") or 0) or None,
        )])
        print(f"  NIFTY 9:15 candle saved: O={nifty_candle['open']} C={nifty_candle['close']}")
    else:
        print("  [WARN] NIFTY 9:15 candle not found.")

    # Step 2: GIFT NIFTY 9:15 candle
    gift_candle = _fetch_915_candle_kite(kc, GIFT_NIFTY_TOKEN, trade_date)
    if gift_candle:
        db.upsert_gift_nifty_snapshots([(
            trade_date,
            float(gift_candle["open"]), float(gift_candle["high"]),
            float(gift_candle["low"]),  float(gift_candle["close"]),
            now,
        )])
        print(f"  GIFT NIFTY 9:15 candle saved: close_920={gift_candle['close']}")
    else:
        print("  [WARN] GIFT NIFTY 9:15 candle not found.")

    if not nifty_candle or not gift_candle:
        return {"trade_date": str(trade_date), "status": "missing_candles"}

    # Step 3: compute and upsert features for signal_date D-1
    signal_date = db.get_previous_trading_day(trade_date, exchange="NSE")
    if signal_date is None:
        print("  [WARN] Could not determine previous trading day.")
        return {"trade_date": str(trade_date), "status": "no_prev_day"}

    prev = _load_prev_day_data(db, signal_date)
    if prev is None:
        print(f"  [WARN] No UnderlyingSnapshot data for {signal_date}")
        return {"trade_date": str(trade_date), "status": "no_prev_data"}

    feats = _compute_features(
        nifty_open=float(nifty_candle["open"]),
        nifty_close=float(nifty_candle["close"]),
        gift_close=float(gift_candle["close"]),
        prev_close=prev["close"],
        atr14=prev["atr14"],
        close_1515_d1=prev["close_1515"],
    )
    feats["symbol"] = "NIFTY"
    feats["signal_date"] = signal_date
    db.upsert_open_gap_features([feats])
    print(f"  Gap features upserted for signal_date={signal_date}: "
          f"gap={feats['nifty_gap_pct']:.4%} drift={feats['nifty_drift_pct']:.4%} "
          f"gift={feats['gift_gap_pct']:.4%} g={feats['gap_open_atr']} gift_atr={feats['gift_gap_atr']}")
    return {"trade_date": str(trade_date), "signal_date": str(signal_date),
            "status": "ok", **feats}


# ── backfill mode: read from DB ───────────────────────────────────────────────

def _load_prev_day_data(db: SupabaseDatabaseClient, signal_date: date) -> dict | None:
    """Load close_1515 from UnderlyingSnapshot and atr14 from SignalFeatureDaily for D-1."""
    with db.conn.cursor() as cur:
        cur.execute(
            'SELECT close_price FROM "UnderlyingSnapshot" '
            "WHERE underlying = 'NIFTY' AND trade_date = %s",
            (signal_date,),
        )
        snap = cur.fetchone()
        cur.execute(
            'SELECT atr14, close_1515 FROM "SignalFeatureDaily" '
            "WHERE symbol = 'NIFTY' AND signal_date = %s",
            (signal_date,),
        )
        feat = cur.fetchone()
    if snap is None:
        return None
    return {
        "close":    float(snap[0]),
        "atr14":    float(feat[0]) if feat and feat[0] is not None else None,
        "close_1515": float(feat[1]) if feat and feat[1] is not None else None,
    }


def run_backfill(
    db: SupabaseDatabaseClient,
    start_date: date,
    end_date: date,
) -> dict:
    """Read candles from UnderlyingCandle5m / GiftNiftySnapshot, compute and upsert."""
    print(f"Backfill mode: loading candle data from DB for {start_date} to {end_date} ...")

    # Load NIFTY 9:15 candles
    with db.conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, open_price, close_price
            FROM "UnderlyingCandle5m"
            WHERE underlying = 'NIFTY'
              AND trade_date BETWEEN %s AND %s
              AND candle_time::time = '09:15:00'
            ORDER BY trade_date
            """,
            (start_date, end_date),
        )
        nifty_rows = {r[0]: {"open": float(r[1]), "close": float(r[2])} for r in cur.fetchall()}

    # Load GIFT NIFTY close_920
    with db.conn.cursor() as cur:
        cur.execute(
            'SELECT trade_date, close_920 FROM "GiftNiftySnapshot" '
            "WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (start_date, end_date),
        )
        gift_rows = {r[0]: float(r[1]) for r in cur.fetchall()}

    # Load all TradingCalendar dates (NSE) in range to iterate only trading days
    with db.conn.cursor() as cur:
        cur.execute(
            """
            SELECT calendar_date FROM "TradingCalendar"
            WHERE exchange = 'NSE' AND is_trading_day = true
              AND calendar_date BETWEEN %s AND %s
            ORDER BY calendar_date
            """,
            (start_date, end_date),
        )
        trading_days = [r[0] for r in cur.fetchall()]

    gap_rows: list[dict] = []
    skipped = 0

    for i, trade_date in enumerate(trading_days):
        if trade_date not in nifty_rows or trade_date not in gift_rows:
            skipped += 1
            continue
        signal_date = trading_days[i - 1] if i > 0 else None
        if signal_date is None:
            skipped += 1
            continue

        prev = _load_prev_day_data(db, signal_date)
        if prev is None:
            skipped += 1
            continue

        nc = nifty_rows[trade_date]
        feats = _compute_features(
            nifty_open=nc["open"],
            nifty_close=nc["close"],
            gift_close=gift_rows[trade_date],
            prev_close=prev["close"],
            atr14=prev["atr14"],
            close_1515_d1=prev["close_1515"],
        )
        feats["symbol"] = "NIFTY"
        feats["signal_date"] = signal_date
        gap_rows.append(feats)

    print(f"  {len(gap_rows)} rows to upsert, {skipped} skipped (missing candle / prev data)")
    if gap_rows:
        upserted = db.upsert_open_gap_features(gap_rows)
        print(f"  Upserted {upserted} rows into SignalFeatureDaily.")
    return {"backfill": True, "rows": len(gap_rows), "skipped": skipped}


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch 9:15 AM candles and compute open-gap features for SignalFeatureDaily."
    )
    parser.add_argument("--start", default=None, help="Backfill start YYYY-MM-DD")
    parser.add_argument("--end",   default=None, help="Backfill end YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    settings = get_settings()
    db = SupabaseDatabaseClient(settings)
    db.connect()

    try:
        if args.start:
            start = date.fromisoformat(args.start)
            end   = date.fromisoformat(args.end) if args.end else date.today()
            result = run_backfill(db, start, end)
        else:
            trade_date = date.today()
            print(f"Live run for trade_date={trade_date}")
            kc = KiteClient(settings)
            kc.authenticate()
            result = run_live(db, kc, trade_date)
    finally:
        db.close()

    print(result)


if __name__ == "__main__":
    main()
