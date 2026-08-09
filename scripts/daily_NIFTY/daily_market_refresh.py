"""
Daily NIFTY/index underlying OHLC refresh.

This is the morning/manual entrypoint for:
  - UnderlyingSnapshot daily OHLC rows
  - SignalFeatureDaily rows via scripts/Common/calculate_underlying_features.py
  - NIFTY futures volume from the NSE FO bhavcopy via
    scripts/backfill_NIFTY/backfill_nifty_volume.py (Kite returns 0 index volume)
  - India VIX into MacroFactorDaily via
    scripts/backfill_NIFTY/backfill_india_vix.py (regime router input)

Default date behavior:
  - Morning/pre-close run: refresh yesterday.
  - After market close: refresh today.
  - --lookback N extends the start date backward N calendar days from the target.

Usage:
    python scripts/daily_NIFTY/daily_market_refresh.py
    python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY
    python scripts/daily_NIFTY/daily_market_refresh.py --underlying ALL
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.kite_client import KiteClient


IST = pytz.timezone("Asia/Kolkata")
MARKET_CLOSE_IST = dtime(15, 35)


def _default_end_date() -> date:
    now_ist = datetime.now(IST)
    if now_ist.time() >= MARKET_CLOSE_IST:
        return now_ist.date()
    return now_ist.date() - timedelta(days=1)


def _resolve_date_range(
    start_raw: str | None,
    end_raw: str | None,
    lookback: int,
) -> tuple[date, date]:
    if lookback < 1:
        raise ValueError("--lookback must be >= 1")

    if start_raw or end_raw:
        if not start_raw or not end_raw:
            raise ValueError("--start and --end must be provided together")
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
    else:
        end_date = _default_end_date()
        start_date = end_date - timedelta(days=lookback - 1)

    if start_date > end_date:
        raise ValueError("start date must be <= end date")
    return start_date, end_date


def run_daily_market_refresh(
    start_date: date,
    end_date: date,
    underlyings: list[str] | None = None,
) -> dict:
    settings = get_settings()
    if not (settings.database_provider == "supabase" or settings.supabase_conn_str):
        raise RuntimeError("daily_market_refresh.py now supports the Supabase NIFTY pipeline only.")
    return run_supabase_daily_market_refresh(start_date, end_date, underlyings)


def run_supabase_daily_market_refresh(
    start_date: date,
    end_date: date,
    underlyings: list[str] | None = None,
) -> dict:
    settings = get_settings()
    kite_client = KiteClient(settings)
    kite_client.authenticate()

    db = get_database_client(settings)
    db.connect()
    try:
        if hasattr(db, "create_core_tables"):
            db.create_core_tables()
        watched = db.get_watched_instruments(instrument_type="INDEX")
        symbols = _resolve_underlyings(watched, underlyings)
        watched_by_symbol = {w.tradingsymbol.upper(): w for w in watched}

        loaded_at = datetime.now(IST).replace(tzinfo=None)
        rows = []
        missing_tokens = []
        for symbol in symbols:
            watched_row = watched_by_symbol.get(symbol)
            if not watched_row or not watched_row.instrument_token:
                missing_tokens.append(symbol)
                continue
            candles = kite_client.kite.historical_data(
                watched_row.instrument_token,
                datetime.combine(start_date, dtime(9, 15)),
                datetime.combine(end_date, dtime(15, 30)),
                interval="day",
                continuous=False,
                oi=False,
            )
            for c in candles:
                c_dt = c["date"].replace(tzinfo=None)
                rows.append((
                    symbol,
                    c_dt.date(),
                    loaded_at,
                    float(c["open"]) if c.get("open") is not None else None,
                    float(c["high"]) if c.get("high") is not None else None,
                    float(c["low"]) if c.get("low") is not None else None,
                    float(c["close"]) if c.get("close") is not None else None,
                    int(c["volume"]) if c.get("volume") is not None else None,
                ))
        if missing_tokens:
            raise RuntimeError(
                "Missing active WatchedInstrument instrument_token for: "
                + ", ".join(missing_tokens)
            )
        summary = db.upsert_underlying_snapshots(rows)
    finally:
        db.close()

    from scripts.Common.calculate_underlying_features import run_calculate_underlying_features
    feature_summary = run_calculate_underlying_features(
        start_date=start_date,
        end_date=end_date,
        underlyings=symbols,
    )

    # Kite returns 0 volume for index OHLC, so backfill NIFTY futures volume from
    # the NSE FO bhavcopy and recompute the rolling volume windows. This is an
    # external HTTP call; never let an NSE outage fail the daily refresh.
    volume_summary: dict | None = None
    if "NIFTY" in symbols:
        try:
            from scripts.backfill_NIFTY.backfill_nifty_volume import run_volume_backfill
            volume_summary = run_volume_backfill(start_date, end_date)
        except Exception as exc:  # noqa: BLE001 - resilience for the cron job
            print(f"[WARN] NIFTY volume backfill skipped: {exc}")
            volume_summary = {"error": str(exc)}

    # India VIX is the regime router input but is not part of the index OHLC
    # candles, so fetch it from Kite into MacroFactorDaily. Resilient like the
    # volume step: a Kite/VIX hiccup must not fail the daily refresh.
    vix_summary: dict | None = None
    try:
        from scripts.backfill_NIFTY.backfill_india_vix import run_backfill_india_vix
        vix_summary = run_backfill_india_vix(start_date, end_date)
    except Exception as exc:  # noqa: BLE001 - resilience for the cron job
        print(f"[WARN] India VIX backfill skipped: {exc}")
        vix_summary = {"error": str(exc)}

    return {
        "provider": "supabase",
        "underlyings": symbols,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "underlying_snapshot": summary,
        "signal_features": feature_summary,
        "nifty_volume": volume_summary,
        "india_vix": vix_summary,
    }


def _resolve_underlyings(watched: list, underlyings: list[str] | None) -> list[str]:
    if not underlyings:
        return ["NIFTY"]
    requested = [u.strip().upper() for u in underlyings if u and u.strip()]
    if "ALL" in requested:
        return [w.tradingsymbol.upper() for w in watched if w.tradingsymbol]
    return requested


def main() -> None:
    load_dotenv(project_root / ".env")

    parser = argparse.ArgumentParser(
        description="Refresh daily underlying OHLC rows and chained SignalFeatureDaily rows."
    )
    parser.add_argument("--start", help="Start date YYYY-MM-DD. Must be used with --end.")
    parser.add_argument("--end", help="End date YYYY-MM-DD. Must be used with --start.")
    parser.add_argument(
        "--lookback",
        type=int,
        default=1,
        help="Calendar days to refresh when --start/--end are omitted. Default: 1.",
    )
    parser.add_argument(
        "--underlying",
        action="append",
        dest="underlyings",
        help="Underlying symbol. Default: NIFTY. Use ALL for every active INDEX in WatchedInstrument.",
    )
    args = parser.parse_args()

    start_date, end_date = _resolve_date_range(args.start, args.end, args.lookback)
    symbols = [s.strip().upper() for s in args.underlyings] if args.underlyings else None

    print(f"Daily market refresh: {start_date} to {end_date}")
    print(f"Underlyings: {symbols or 'all active WatchedInstrument rows'}")

    result = run_daily_market_refresh(
        start_date=start_date,
        end_date=end_date,
        underlyings=symbols,
    )

    print("\nRefresh complete.")
    print(result)

    # ── Chain: re-run prediction pipeline so actual_trade_label is graded ──────
    # Use a 5-calendar-day lookback so at least 2 prior trading days are
    # re-graded (covers the newly-arrived today OHLC resolving yesterday's label).
    import subprocess
    pred_start = (end_date - timedelta(days=5)).isoformat()
    pred_end   = end_date.isoformat()
    print(f"\n── Chaining prediction pipeline {pred_start} → {pred_end} ──")
    pred_proc = subprocess.run(
        [sys.executable,
         str(project_root / "scripts" / "daily_NIFTY" / "daily_nifty_prediction.py"),
         "--start", pred_start, "--end", pred_end],
        capture_output=True, text=True, cwd=str(project_root),
    )
    print((pred_proc.stdout or "").strip()[-3000:])
    if pred_proc.returncode != 0:
        print(f"[WARN] Prediction pipeline exited {pred_proc.returncode}:\n"
              f"{(pred_proc.stderr or '').strip()[-800:]}")
    else:
        print("Prediction pipeline complete.")


if __name__ == "__main__":
    main()
