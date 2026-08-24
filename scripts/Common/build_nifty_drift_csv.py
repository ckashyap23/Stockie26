"""
Build NIFTY_drift.csv — per-day 5-minute candle summary since 2024.

Columns:
  signal_date          — trading date
  O, H, L, C          — day-level prices (O=9:15 open, H=day high, L=day low, C=last candle close)
  high_time            — HH:MM of first candle whose high_price == day high
  low_time             — HH:MM of first candle whose low_price == day low
  high_pct             — (H - O) / O
  low_pct              — (L - O) / O
  9_20_close …         — close_price of each 5-min candle, labeled by END time
  … 3_30_close         — close of 15:25 candle (== daily close)

PM hour convention: hours 13/14/15 are labeled as 1/2/3 to match market parlance
(e.g. 15:30 → 3_30_close).

Only dates where at least the first candle (09:15) is present are included.
Afternoon candles that are missing are left as NaN (no date is dropped for that).
"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.supabase_client import SupabaseDatabaseClient

OUTPUT_PATH = project_root / "output" / "feature_store" / "NIFTY_drift.csv"


# ── build full candle-start → column-name map (9:15 → 15:25, 5-min steps) ───
def _col_name(end_h: int, end_m: int) -> str:
    """Column name for the candle that CLOSES at end_h:end_m (24-hr).
    Hours 13/14/15 are shortened to 1/2/3 (market parlance).
    """
    display_h = end_h - 12 if end_h >= 13 else end_h
    if end_m == 0:
        return f"{display_h}_close"
    return f"{display_h}_{end_m:02d}_close"


def _build_candle_col_map() -> dict[dtime, str]:
    mapping: dict[dtime, str] = {}
    start = datetime(2000, 1, 1, 9, 15)
    end   = datetime(2000, 1, 1, 15, 25)   # last candle START time
    cur = start
    while cur <= end:
        close_t = cur + timedelta(minutes=5)
        col = _col_name(close_t.hour, close_t.minute)
        mapping[cur.time()] = col
        cur += timedelta(minutes=5)
    return mapping


CANDLE_COL_MAP = _build_candle_col_map()
MORNING_REQUIRED = {dtime(9, 15)}   # only require the very first candle to include a date

# ── load candles ──────────────────────────────────────────────────────────────
settings = get_settings()
db = SupabaseDatabaseClient(settings)
db.connect()

print("Loading candle data from DB…")
with db.conn.cursor() as cur:
    cur.execute(
        'SELECT trade_date, candle_time, open_price, high_price, low_price, close_price '
        'FROM "UnderlyingCandle5m" '
        "WHERE underlying = %s AND trade_date >= %s "
        "ORDER BY trade_date, candle_time",
        ("NIFTY", "2024-01-01"),
    )
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()

db.close()

df = pd.DataFrame(rows, columns=cols)
df["trade_date"]      = pd.to_datetime(df["trade_date"]).dt.date
df["candle_time_only"] = pd.to_datetime(df["candle_time"]).dt.time
print(f"  {len(df):,} candle rows across {df['trade_date'].nunique()} dates")

# ── process each trading day ──────────────────────────────────────────────────
records = []
skipped = 0

for trade_date, day in df.groupby("trade_date"):
    day = day.sort_values("candle_time")
    day_times = set(day["candle_time_only"])

    # Skip if we don't even have the opening candle
    if not MORNING_REQUIRED.issubset(day_times):
        skipped += 1
        continue

    # Day-level OHLC from candles
    first_row = day[day["candle_time_only"] == dtime(9, 15)].iloc[0]
    O = float(first_row["open_price"])
    H = float(day["high_price"].max())
    L = float(day["low_price"].min())
    C = float(day.iloc[-1]["close_price"])

    # high_time / low_time
    high_rows = day[day["high_price"] == H]
    low_rows  = day[day["low_price"]  == L]
    high_time = pd.to_datetime(high_rows.iloc[0]["candle_time"]).strftime("%H:%M") if not high_rows.empty else None
    low_time  = pd.to_datetime(low_rows.iloc[0]["candle_time"]).strftime("%H:%M")  if not low_rows.empty  else None

    high_pct = round((H - O) / O, 6) if O else None
    low_pct  = round((L - O) / O, 6) if O else None

    # All intraday candle closes (NaN when candle absent)
    candle_closes: dict[str, float | None] = {}
    for ct, col_name in CANDLE_COL_MAP.items():
        row = day[day["candle_time_only"] == ct]
        candle_closes[col_name] = float(row.iloc[0]["close_price"]) if not row.empty else None

    records.append({
        "signal_date": str(trade_date),
        "O": O, "H": H, "L": L, "C": C,
        "high_time": high_time,
        "low_time":  low_time,
        "high_pct":  high_pct,
        "low_pct":   low_pct,
        **candle_closes,
    })

# ── write output ──────────────────────────────────────────────────────────────
# Preserve column order: fixed cols first, then candles in time order
fixed_cols = ["signal_date", "O", "H", "L", "C", "high_time", "low_time", "high_pct", "low_pct"]
candle_cols = list(CANDLE_COL_MAP.values())   # already in chronological order

out = pd.DataFrame(records, columns=fixed_cols + candle_cols)
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
out.to_csv(OUTPUT_PATH, index=False)

print(f"\nWritten {len(out)} rows ({skipped} skipped) → {OUTPUT_PATH}")
print(f"Date range : {out['signal_date'].min()} → {out['signal_date'].max()}")
print(f"Columns    : {len(out.columns)} total ({len(candle_cols)} intraday closes)")
print(f"First col  : {candle_cols[0]}   Last col: {candle_cols[-1]}")
print("\nSample (first 2 rows, first 15 cols):")
print(out.iloc[:2, :15].to_string(index=False))
