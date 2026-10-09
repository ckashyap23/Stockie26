"""Backfill NIFTY option PCR metrics into SignalFeatureDaily.

Source: NSE FO daily bhavcopy (UDiFF).

For each trade date:
  - Aggregate NIFTY index-option CE/PE totals from FO rows.
  - pcr_oi = total_put_oi / total_call_oi
  - pcr_volume = total_put_volume / total_call_volume
  - Write to SignalFeatureDaily for symbol='NIFTY'.
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
_FO_OPTIONS_TYPE = "IDO"  # Index Derivatives Options
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
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    try:
        sess.get("https://www.nseindia.com", timeout=15)
    except Exception:
        pass
    return sess


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


def extract_nifty_option_totals(df: pd.DataFrame) -> dict[str, float] | None:
    """Return aggregated CE/PE OI and volume totals for NIFTY index options."""
    required = {"FinInstrmTp", "TckrSymb", "OptnTp", "OpnIntrst", "TtlTradgVol"}
    if not required.issubset(df.columns):
        return None

    rows = df[
        (df["FinInstrmTp"].astype(str).str.upper() == _FO_OPTIONS_TYPE)
        & (df["TckrSymb"].astype(str).str.upper() == _UNDERLYING)
        & (df["OptnTp"].astype(str).str.upper().isin(["CE", "PE"]))
    ].copy()
    if rows.empty:
        return None

    rows["OpnIntrst"] = pd.to_numeric(rows["OpnIntrst"], errors="coerce").fillna(0.0)
    rows["TtlTradgVol"] = pd.to_numeric(rows["TtlTradgVol"], errors="coerce").fillna(0.0)
    rows["OptnTp"] = rows["OptnTp"].astype(str).str.upper()

    ce = rows[rows["OptnTp"] == "CE"]
    pe = rows[rows["OptnTp"] == "PE"]

    call_oi = float(ce["OpnIntrst"].sum())
    put_oi = float(pe["OpnIntrst"].sum())
    call_vol = float(ce["TtlTradgVol"].sum())
    put_vol = float(pe["TtlTradgVol"].sum())

    return {
        "call_oi": call_oi,
        "put_oi": put_oi,
        "call_vol": call_vol,
        "put_vol": put_vol,
        "pcr_oi": (put_oi / call_oi) if call_oi > 0 else None,
        "pcr_volume": (put_vol / call_vol) if call_vol > 0 else None,
    }


def _ensure_pcr_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE "SignalFeatureDaily"
                ADD COLUMN IF NOT EXISTS pcr_oi double precision,
                ADD COLUMN IF NOT EXISTS pcr_volume double precision
            """
        )
    conn.commit()


def _update_signal_pcr(conn, trade_date: date, pcr_oi: float | None, pcr_volume: float | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE "SignalFeatureDaily"
            SET pcr_oi = %s,
                pcr_volume = %s,
                updated_at = NOW()
            WHERE symbol = %s
              AND signal_date = %s
              AND feature_version = 'v1'
            """,
            (pcr_oi, pcr_volume, _UNDERLYING, trade_date),
        )
        count = cur.rowcount
    conn.commit()
    return count


def run_backfill_nifty_option_pcr(start_date: date, end_date: date) -> dict[str, Any]:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    _ensure_pcr_columns(db.conn)

    sess = _build_session()
    days = _trading_days(start_date, end_date)

    bhavcopy_found = 0
    with_totals = 0
    updated_rows = 0

    print(f"Downloading NSE FO bhavcopy for {len(days)} candidate trading days ...")
    for d in days:
        bhavcopy = _fetch_bhavcopy(sess, d)
        if bhavcopy is None:
            continue
        bhavcopy_found += 1

        totals = extract_nifty_option_totals(bhavcopy)
        if totals is None:
            continue
        with_totals += 1

        updated_rows += _update_signal_pcr(
            db.conn,
            d,
            totals["pcr_oi"],
            totals["pcr_volume"],
        )

    db.close()
    return {
        "candidate_days": len(days),
        "bhavcopy_found": bhavcopy_found,
        "days_with_nifty_option_totals": with_totals,
        "signal_rows_updated": updated_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill NIFTY option PCR features from NSE FO bhavcopy.")
    parser.add_argument("--start", default="2026-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=str(date.today()), help="End date YYYY-MM-DD")
    args = parser.parse_args()

    result = run_backfill_nifty_option_pcr(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
    )
    print(result)


if __name__ == "__main__":
    main()
