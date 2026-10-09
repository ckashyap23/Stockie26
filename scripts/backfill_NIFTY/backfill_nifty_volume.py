"""
backfill_nifty_volume.py

Downloads NSE FO daily bhavcopy (UDiFF format), extracts NIFTY near-month
futures volume (TtlTradgVol), and writes it to Supabase:

  1. UPDATE UnderlyingSnapshot.volume  for each trade_date
  2. Recompute rolling volume averages and update SignalFeatureDaily.volume_day,`r`n     volume_10d, and volume_20d

Run:
    python scripts/backfill_NIFTY/backfill_nifty_volume.py --start 2026-01-01 --end 2026-06-17

After this script completes, re-run the prediction backtest to pick up volume windows:
    python -m src.technical_analysis.cascade.pipeline
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

load_dotenv(_repo_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client

_UNDERLYING = "NIFTY"
_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{date:%Y%m%d}_F_0000.csv.zip"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def _trading_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri; holidays handled by missing bhavcopy files
            days.append(d)
        d += timedelta(days=1)
    return days


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=15)
    except Exception:
        pass
    return s


def _fetch_bhavcopy(sess: requests.Session, d: date) -> pd.DataFrame | None:
    url = _BHAVCOPY_URL.format(date=d)
    try:
        resp = sess.get(url, timeout=40)
        if resp.status_code != 200 or not resp.content:
            return None
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
        if not csv_name:
            return None
        with zf.open(csv_name) as f:
            return pd.read_csv(f)
    except Exception:
        return None


def _extract_nifty_futures_volume(df: pd.DataFrame, trade_date: date) -> int | None:
    """Return near-month NIFTY index futures traded volume, or None if not found."""
    required = {"FinInstrmTp", "TckrSymb", "XpryDt", "TtlTradgVol"}
    if not required.issubset(df.columns):
        return None

    rows = df[
        (df["FinInstrmTp"].astype(str).str.upper() == "IDF")
        & (df["TckrSymb"].astype(str).str.upper() == _UNDERLYING)
    ].copy()
    if rows.empty:
        return None

    rows["XpryDt"] = pd.to_datetime(rows["XpryDt"], errors="coerce").dt.date
    rows = rows.dropna(subset=["XpryDt"])
    rows = rows[rows["XpryDt"] >= trade_date].sort_values("XpryDt")
    if rows.empty:
        return None

    vol = rows.iloc[0]["TtlTradgVol"]
    try:
        return int(float(vol)) if pd.notna(vol) else None
    except (ValueError, TypeError):
        return None


def _update_underlying_snapshot_volume(conn, trade_date: date, volume: int) -> None:
    sql = """
        UPDATE "UnderlyingSnapshot"
        SET volume = %s
        WHERE underlying = %s AND trade_date = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (volume, _UNDERLYING, trade_date))
    conn.commit()


def _fetch_all_ohlcv(conn) -> pd.DataFrame:
    sql = """
        SELECT trade_date, volume
        FROM "UnderlyingSnapshot"
        WHERE underlying = %s
        ORDER BY trade_date
    """
    with conn.cursor() as cur:
        cur.execute(sql, (_UNDERLYING,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    df = pd.DataFrame(rows, columns=cols)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df


def _compute_and_write_volume_windows(conn, ohlcv_df: pd.DataFrame) -> int:
    """Compute rolling volume averages, write volume_day + volume_10d/20d."""
    df = ohlcv_df.copy().sort_values("trade_date").reset_index(drop=True)
    df["volume_10d"] = df["volume"].rolling(window=10, min_periods=10).mean()
    df["volume_20d"] = df["volume"].rolling(window=20, min_periods=20).mean()

    updated = 0
    sql = """
        UPDATE "SignalFeatureDaily"
        SET volume_day  = %s,
            volume_10d  = %s,
            volume_20d  = %s,
            updated_at  = NOW()
        WHERE symbol = %s AND signal_date = %s AND feature_version = 'v1'
    """
    with conn.cursor() as cur:
        for _, row in df.iterrows():
            volume_10d = None if pd.isna(row["volume_10d"]) else round(float(row["volume_10d"]), 4)
            volume_20d = None if pd.isna(row["volume_20d"]) else round(float(row["volume_20d"]), 4)
            # Preserve explicit zero from source instead of degrading to NULL.
            vol_day = int(row["volume"]) if pd.notna(row["volume"]) else None
            cur.execute(sql, (vol_day, volume_10d, volume_20d, _UNDERLYING, row["trade_date"]))
            if cur.rowcount:
                updated += 1
    conn.commit()
    return updated


def run_volume_backfill(start_date: date, end_date: date) -> dict[str, Any]:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()

    sess = _build_session()
    days = _trading_days(start_date, end_date)
    volume_found: dict[date, int] = {}

    print(f"Downloading NSE FO bhavcopy for {len(days)} candidate trading days ...")
    for d in days:
        bhavcopy = _fetch_bhavcopy(sess, d)
        if bhavcopy is None:
            print(f"  {d}: no bhavcopy (holiday or missing)")
            continue
        vol = _extract_nifty_futures_volume(bhavcopy, d)
        if vol is None:
            print(f"  {d}: NIFTY futures row not found in bhavcopy")
            continue
        _update_underlying_snapshot_volume(db.conn, d, vol)
        volume_found[d] = vol
        print(f"  {d}: volume = {vol:,}")

    print(f"\nUpdated {len(volume_found)} UnderlyingSnapshot rows.")
    print("Recomputing volume windows in SignalFeatureDaily ...")
    ohlcv_df = _fetch_all_ohlcv(db.conn)
    updated = _compute_and_write_volume_windows(db.conn, ohlcv_df)
    print(f"Updated {updated} SignalFeatureDaily rows with volume_day / volume_10d / volume_20d.")

    db.close()
    return {
        "dates_with_volume": len(volume_found),
        "signal_feature_rows_updated": updated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill NIFTY futures volume into Supabase.")
    parser.add_argument("--start", default="2026-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=str(date.today()), help="End date YYYY-MM-DD")
    args = parser.parse_args()
    result = run_volume_backfill(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
    )
    print(result)


if __name__ == "__main__":
    main()


