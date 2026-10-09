"""Backfill NIFTY futures OI and oi_regime into SignalFeatureDaily.

Source: NSE derivatives bhavcopy archives.
Primary URL pattern requested by user:
  https://archives.nseindia.com/content/historical/DERIVATIVES/{year}/{month}/fo{ddmmyyyy}bhav.csv.zip

For each trade date:
  - select near-month NIFTY futures contract
  - futures_oi[t] = OPEN_INT (or OpnIntrst in UDiFF schema)
Then classify:
  price_chg[t] = close_1515[t] - close_1515[t-1]
  oi_chg[t]    = futures_oi[t] - futures_oi[t-1]
  oi_regime[t] in {long_buildup, short_covering, short_buildup, long_unwinding}
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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client

_UNDERLYING = "NIFTY"
_LEGACY_URL = "https://archives.nseindia.com/content/historical/DERIVATIVES/{year}/{month}/fo{ddmmyyyy}bhav.csv.zip"
_UDIFF_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
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
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=15)
    except Exception:
        pass
    return s


def _read_zip_csv(content: bytes) -> pd.DataFrame | None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
        if not csv_name:
            return None
        with zf.open(csv_name) as f:
            # Keep default parser flexible for both legacy and UDiFF files.
            return pd.read_csv(f)
    except Exception:
        return None


def _fetch_bhavcopy(sess: requests.Session, d: date) -> pd.DataFrame | None:
    legacy = _LEGACY_URL.format(
        year=d.strftime("%Y"),
        month=d.strftime("%b").upper(),
        ddmmyyyy=d.strftime("%d%m%Y"),
    )
    udiff = _UDIFF_URL.format(yyyymmdd=d.strftime("%Y%m%d"))
    for url in (legacy, udiff):
        try:
            r = sess.get(url, timeout=40)
            if r.status_code != 200 or not r.content:
                continue
            df = _read_zip_csv(r.content)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return None


def extract_near_month_futures_oi(df: pd.DataFrame, trade_date: date) -> int | None:
    """Extract near-month NIFTY futures OI from legacy or UDiFF schema."""
    cols = set(df.columns)

    # Legacy schema
    if {"SYMBOL", "INSTRUMENT", "EXPIRY_DT", "OPEN_INT"}.issubset(cols):
        rows = df[
            (df["SYMBOL"].astype(str).str.upper() == _UNDERLYING)
            & (df["INSTRUMENT"].astype(str).str.upper() == "FUTIDX")
        ].copy()
        if rows.empty:
            return None
        rows["EXPIRY_DT"] = pd.to_datetime(rows["EXPIRY_DT"], errors="coerce").dt.date
        rows = rows.dropna(subset=["EXPIRY_DT"])
        rows = rows[rows["EXPIRY_DT"] >= trade_date].sort_values("EXPIRY_DT")
        if rows.empty:
            return None
        oi_val = pd.to_numeric(rows.iloc[0]["OPEN_INT"], errors="coerce")
        return int(float(oi_val)) if pd.notna(oi_val) else None

    # UDiFF schema fallback
    if {"TckrSymb", "FinInstrmTp", "XpryDt", "OpnIntrst"}.issubset(cols):
        rows = df[
            (df["TckrSymb"].astype(str).str.upper() == _UNDERLYING)
            & (df["FinInstrmTp"].astype(str).str.upper() == "IDF")
        ].copy()
        if rows.empty:
            return None
        rows["XpryDt"] = pd.to_datetime(rows["XpryDt"], errors="coerce").dt.date
        rows = rows.dropna(subset=["XpryDt"])
        rows = rows[rows["XpryDt"] >= trade_date].sort_values("XpryDt")
        if rows.empty:
            return None
        oi_val = pd.to_numeric(rows.iloc[0]["OpnIntrst"], errors="coerce")
        return int(float(oi_val)) if pd.notna(oi_val) else None

    return None


def _ensure_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE "SignalFeatureDaily"
                ADD COLUMN IF NOT EXISTS futures_oi bigint,
                ADD COLUMN IF NOT EXISTS oi_regime varchar(40)
            """
        )
    conn.commit()


def _update_futures_oi(conn, trade_date: date, futures_oi: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE "SignalFeatureDaily"
            SET futures_oi = %s,
                updated_at = NOW()
            WHERE symbol = %s
              AND feature_version = 'v1'
              AND signal_date = %s
            """,
            (futures_oi, _UNDERLYING, trade_date),
        )
        count = cur.rowcount
    conn.commit()
    return count


def _recompute_oi_regime(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ordered AS (
                SELECT
                    signal_date,
                    close_1515,
                    futures_oi,
                    LAG(close_1515) OVER (ORDER BY signal_date) AS prev_close,
                    LAG(futures_oi) OVER (ORDER BY signal_date) AS prev_oi
                FROM "SignalFeatureDaily"
                WHERE symbol = %s
                  AND feature_version = 'v1'
            )
            UPDATE "SignalFeatureDaily" s
            SET oi_regime = CASE
                WHEN o.close_1515 IS NULL OR o.prev_close IS NULL OR o.futures_oi IS NULL OR o.prev_oi IS NULL THEN NULL
                WHEN (o.close_1515 - o.prev_close) > 0 AND (o.futures_oi - o.prev_oi) > 0 THEN 'long_buildup'
                WHEN (o.close_1515 - o.prev_close) > 0 AND (o.futures_oi - o.prev_oi) < 0 THEN 'short_covering'
                WHEN (o.close_1515 - o.prev_close) < 0 AND (o.futures_oi - o.prev_oi) > 0 THEN 'short_buildup'
                WHEN (o.close_1515 - o.prev_close) < 0 AND (o.futures_oi - o.prev_oi) < 0 THEN 'long_unwinding'
                ELSE NULL
            END,
            updated_at = NOW()
            FROM ordered o
            WHERE s.symbol = %s
              AND s.feature_version = 'v1'
              AND s.signal_date = o.signal_date
            """,
            (_UNDERLYING, _UNDERLYING),
        )
        count = cur.rowcount
    conn.commit()
    return count


def run_backfill_nifty_oi_regime(start_date: date, end_date: date) -> dict[str, Any]:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    _ensure_columns(db.conn)

    sess = _build_session()
    days = _trading_days(start_date, end_date)

    found_files = 0
    found_oi = 0
    updated_rows = 0

    print(f"Downloading NSE derivatives bhavcopy for {len(days)} candidate trading days ...")
    for d in days:
        df = _fetch_bhavcopy(sess, d)
        if df is None:
            continue
        found_files += 1
        oi = extract_near_month_futures_oi(df, d)
        if oi is None:
            continue
        found_oi += 1
        updated_rows += _update_futures_oi(db.conn, d, oi)

    regime_rows = _recompute_oi_regime(db.conn)
    db.close()
    return {
        "candidate_days": len(days),
        "bhavcopy_found": found_files,
        "days_with_near_month_oi": found_oi,
        "signal_rows_futures_oi_updated": updated_rows,
        "signal_rows_oi_regime_recomputed": regime_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill NIFTY futures OI and oi_regime from NSE bhavcopy")
    parser.add_argument("--start", default="2026-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=str(date.today()), help="End date YYYY-MM-DD")
    args = parser.parse_args()

    result = run_backfill_nifty_oi_regime(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
    )
    print(result)


if __name__ == "__main__":
    main()
