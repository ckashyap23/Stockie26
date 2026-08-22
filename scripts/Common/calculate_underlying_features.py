# scripts/Common/calculate_underlying_features.py
"""
Compute technical features from UnderlyingSnapshot OHLCV and persist to SignalFeatureDaily.

Called automatically after UnderlyingSnapshot rows are written by:
  - daily_market_refresh.py
  - backfill_underlying.py

Can also be run standalone for a date range.

Usage:
    python scripts/Common/calculate_underlying_features.py
    python scripts/Common/calculate_underlying_features.py --start 2026-01-01 --end 2026-06-15
    python scripts/Common/calculate_underlying_features.py --underlying NIFTY --start 2026-06-01 --end 2026-06-15
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.technical_analysis.prediction.features import compute_underlying_features

LOOKBACK_DAYS = 120  # days of OHLCV history needed to compute all features (ma90 + buffer)


def fetch_ohlcv(db, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    fetch_start = start_date - timedelta(days=LOOKBACK_DAYS)
    is_postgres = getattr(db, "db_kind", "") == "postgres"
    table = '"UnderlyingSnapshot"' if is_postgres else "dbo.UnderlyingSnapshot"
    ph = "%s" if is_postgres else "?"
    sql = f"""
        SELECT trade_date, open_price, high_price, low_price, close_price, volume
        FROM {table}
        WHERE underlying = {ph}
          AND trade_date >= {ph}
          AND trade_date <= {ph}
        ORDER BY trade_date
    """
    df = pd.read_sql(sql, db.conn, params=[symbol, fetch_start, end_date])
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df.sort_values("trade_date").reset_index(drop=True)


def compute_features_for_symbol(
    db,
    symbol: str,
    start_date: date,
    end_date: date,
    feature_version: str = "v1",
) -> int:
    df = fetch_ohlcv(db, symbol, start_date, end_date)
    if df.empty:
        print(f"  {symbol}: no OHLCV data found")
        return 0

    target_dates = df[df["trade_date"].between(start_date, end_date)]["trade_date"].tolist()
    if not target_dates:
        print(f"  {symbol}: no target dates in range")
        return 0

    rows: list[dict] = []
    for trade_dt in target_dates:
        window = df[df["trade_date"] <= trade_dt].tail(LOOKBACK_DAYS)
        if len(window) < 10:
            continue

        features = compute_underlying_features(window)

        last_row = window.iloc[-1]
        row = {
            "signal_date": trade_dt,
            "symbol": symbol,
            "feature_version": feature_version,
            "close_1515": float(last_row["close_price"]) if last_row.get("close_price") is not None else None,
            "open_915": float(last_row["open_price"]) if last_row.get("open_price") is not None else None,
            "high_day": float(last_row["high_price"]) if last_row.get("high_price") is not None else None,
            "low_day": float(last_row["low_price"]) if last_row.get("low_price") is not None else None,
            "volume_day": int(last_row["volume"]) if last_row.get("volume") is not None else None,
        }
        row.update(features)
        rows.append(row)

    if not rows:
        return 0

    db.upsert_signal_features(rows)
    return len(rows)


def run_calculate_underlying_features(
    start_date: date,
    end_date: date,
    underlyings: list[str] | None = None,
    feature_version: str = "v1",
) -> dict:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()

    try:
        if underlyings:
            symbols = [s.upper() for s in underlyings]
        else:
            watched = db.get_watched_instruments(instrument_type="INDEX")
            symbols = [w.tradingsymbol for w in watched if w.tradingsymbol in ("NIFTY", "BANKNIFTY")]
            if not symbols:
                symbols = ["NIFTY"]

        total = 0
        for symbol in symbols:
            print(f"Computing features for {symbol} ({start_date} to {end_date})...")
            count = compute_features_for_symbol(db, symbol, start_date, end_date, feature_version)
            print(f"  {symbol}: {count} feature rows upserted")
            total += count
    finally:
        db.close()

    return {
        "symbols": symbols,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "feature_version": feature_version,
        "rows_upserted": total,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute underlying features from OHLCV and write to SignalFeatureDaily.")
    parser.add_argument("--start", help="Start date YYYY-MM-DD. Defaults to yesterday.")
    parser.add_argument("--end", help="End date YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--underlying", action="append", dest="underlyings", help="Symbol(s) to process. Can repeat.")
    parser.add_argument("--feature-version", default="v1", help="Feature version tag. Default: v1")
    args = parser.parse_args()

    today = date.today()
    end_date = date.fromisoformat(args.end) if args.end else today
    start_date = date.fromisoformat(args.start) if args.start else end_date - timedelta(days=1)
    symbols = [s.strip().upper() for s in args.underlyings] if args.underlyings else None

    result = run_calculate_underlying_features(
        start_date=start_date,
        end_date=end_date,
        underlyings=symbols,
        feature_version=args.feature_version,
    )
    print("\nFeature calculation complete.")
    print(result)


if __name__ == "__main__":
    main()
