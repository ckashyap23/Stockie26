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


def _safe_read_sql(db, sql: str, params: list) -> pd.DataFrame:
    try:
        return pd.read_sql(sql, db.conn, params=params)
    except Exception:
        return pd.DataFrame()


def fetch_ohlcv(db, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    fetch_start = start_date - timedelta(days=LOOKBACK_DAYS)
    is_postgres = getattr(db, "db_kind", "") == "postgres"
    snap_table = '"UnderlyingSnapshot"' if is_postgres else "dbo.UnderlyingSnapshot"
    candle_table = '"UnderlyingCandle5m"' if is_postgres else "dbo.UnderlyingCandle5m"
    candle_1515_time_expr = "c1515.candle_time::time = '15:15:00'" if is_postgres else "CAST(c1515.candle_time AS time) = '15:15:00'"
    candle_1415_time_expr = "c1415.candle_time::time = '14:15:00'" if is_postgres else "CAST(c1415.candle_time AS time) = '14:15:00'"
    ph = "%s" if is_postgres else "?"
    sql = f"""
        SELECT
            s.trade_date,
            s.open_price,
            s.high_price,
            s.low_price,
                        COALESCE(c1515.close_price, s.close_price) AS close_price,
                        c1415.close_price AS close_1415,
            s.volume,
                        CASE WHEN c1515.close_price IS NOT NULL THEN 'CANDLE_1515' ELSE 'DAILY_OHLC_FALLBACK' END AS close_1515_source
        FROM {snap_table} s
                LEFT JOIN {candle_table} c1515
                    ON c1515.underlying = s.underlying
                 AND c1515.trade_date = s.trade_date
                 AND {candle_1515_time_expr}
                LEFT JOIN {candle_table} c1415
                    ON c1415.underlying = s.underlying
                 AND c1415.trade_date = s.trade_date
                 AND {candle_1415_time_expr}
        WHERE s.underlying = {ph}
          AND s.trade_date >= {ph}
          AND s.trade_date <= {ph}
        ORDER BY s.trade_date
    """
    df = pd.read_sql(sql, db.conn, params=[symbol, fetch_start, end_date])
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df.sort_values("trade_date").reset_index(drop=True)


def fetch_breadth_features_by_date(db, start_date: date, end_date: date) -> dict[date, dict[str, float]]:
    fetch_start = start_date - timedelta(days=LOOKBACK_DAYS)
    is_postgres = getattr(db, "db_kind", "") == "postgres"
    table = '"UnderlyingSnapshot"' if is_postgres else "dbo.UnderlyingSnapshot"
    ph = "%s" if is_postgres else "?"

    watched_constituents = db.get_watched_instruments(instrument_type="NIFTY50")
    constituents = sorted({w.tradingsymbol.upper() for w in watched_constituents if w.is_active})
    if not constituents:
        return {}

    constituent_placeholders = ", ".join([ph for _ in constituents])
    sql = f"""
        SELECT trade_date, underlying, close_price
        FROM {table}
        WHERE trade_date >= {ph}
          AND trade_date <= {ph}
          AND close_price IS NOT NULL
          AND UPPER(underlying) IN ({constituent_placeholders})
        ORDER BY underlying, trade_date
    """
    df = pd.read_sql(sql, db.conn, params=[fetch_start, end_date, *constituents])
    if df.empty:
        return {}

    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["underlying"] = df["underlying"].astype(str).str.upper()
    df = df.sort_values(["underlying", "trade_date"]).reset_index(drop=True)
    df["ret_1d"] = df.groupby("underlying")["close_price"].pct_change()
    df["rolling_high_20d"] = df.groupby("underlying")["close_price"].transform(
        lambda s: s.rolling(20, min_periods=20).max()
    )
    df["is_new_high_20d"] = (df["close_price"] == df["rolling_high_20d"]) & df["rolling_high_20d"].notna()

    constituent_count = float(len(constituents))
    breadth_by_date: dict[date, dict[str, float]] = {}
    for trade_dt, grp in df.groupby("trade_date", sort=True):
        returns = pd.to_numeric(grp["ret_1d"], errors="coerce").dropna()
        advances = int((returns > 0).sum())
        declines = int((returns < 0).sum())
        new_highs_20d = int(grp["is_new_high_20d"].sum())
        breadth_by_date[trade_dt] = {
            "breadth_advance_decline_ratio": float(advances) / float(max(declines, 1)),
            "breadth_new_high_ratio": float(new_highs_20d) / constituent_count,
        }
    return breadth_by_date


def compute_features_for_symbol(
    db,
    symbol: str,
    start_date: date,
    end_date: date,
    banknifty_df: pd.DataFrame | None = None,
    breadth_features_by_date: dict[date, dict[str, float]] | None = None,
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

        window = window.set_index("trade_date", drop=False)
        banknifty_window = None
        if symbol.upper() == "NIFTY" and banknifty_df is not None and not banknifty_df.empty:
            banknifty_window = banknifty_df[banknifty_df["trade_date"] <= trade_dt].tail(LOOKBACK_DAYS)
            if len(banknifty_window) >= 2:
                banknifty_window = banknifty_window.set_index("trade_date", drop=False)
            else:
                banknifty_window = None

        breadth_ratio = None
        breadth_new_high_ratio = None
        if symbol.upper() == "NIFTY" and breadth_features_by_date:
            breadth_for_date = breadth_features_by_date.get(trade_dt)
            if breadth_for_date:
                breadth_ratio = breadth_for_date.get("breadth_advance_decline_ratio")
                breadth_new_high_ratio = breadth_for_date.get("breadth_new_high_ratio")

        features = compute_underlying_features(
            window,
            banknifty_window=banknifty_window,
            breadth_advance_decline_ratio=breadth_ratio,
            breadth_new_high_ratio=breadth_new_high_ratio,
        )

        last_row = window.iloc[-1]
        row = {
            "signal_date": trade_dt,
            "symbol": symbol,
            "feature_version": feature_version,
            "close_1515": float(last_row["close_price"]) if last_row.get("close_price") is not None else None,
            "close_1415": float(last_row["close_1415"]) if last_row.get("close_1415") is not None else None,
            "close_1515_source": str(last_row.get("close_1515_source") or "DAILY_OHLC_FALLBACK"),
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
        banknifty_df = fetch_ohlcv(db, "BANKNIFTY", start_date, end_date) if "NIFTY" in symbols else None
        breadth_features_by_date = fetch_breadth_features_by_date(db, start_date, end_date)
        for symbol in symbols:
            print(f"Computing features for {symbol} ({start_date} to {end_date})...")
            count = compute_features_for_symbol(
                db,
                symbol,
                start_date,
                end_date,
                banknifty_df=banknifty_df,
                breadth_features_by_date=breadth_features_by_date,
                feature_version=feature_version,
            )
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
