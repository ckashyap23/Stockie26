"""
Export NIFTY underlying and option data to separate Excel files for inspection.

Produces two files in --output-dir:
  {underlying}_{start}_{end}_underlying.xlsx  — daily OHLCV joined with all SignalFeatureDaily columns
  {underlying}_{start}_{end}_options.xlsx     — option snapshot + IV/Greeks from OptionSnapshotCalc

Underlying data defaults to 2025-01-01 (features stable from Jan 2025).
Options are always clamped to 2026-04-01 (earliest Supabase option data).

Usage:
    python scripts/Common/export_db_to_excel.py
    python scripts/Common/export_db_to_excel.py --start 2025-01-01 --end 2026-06-17
    python scripts/Common/export_db_to_excel.py --snapshot-label all --output-dir output/db
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client

load_dotenv()

MIN_OPTION_START_DATE = date(2026, 4, 1)   # Supabase option data starts here
MIN_UNDERLYING_START_DATE = date(2025, 1, 1)  # features stable from Jan 2025 onward

_UNDERLYING_QUERY = """
SELECT
    us.trade_date,
    us.underlying,
    us.open_price,
    us.high_price,
    us.low_price,
    us.close_price,
    us.volume,
    sf.ma10,
    sf.ma20,
    sf.ma50,
    sf.ma90,
    sf.rsi14,
    sf.atr7,
    sf.atr7_sma,
    sf.atr14,
    sf.atr14_sma,
    sf.bb_upper,
    sf.bb_middle,
    sf.bb_lower,
    sf.bb_width,
    sf.ret_5d,
    sf.ret_10d,
    sf.ret_20d,
    sf.ret_60d,
    sf.volatility_10d,
    sf.volatility_20d,
    sf.volume_10d,
    sf.volume_20d,
    sf.trend_efficiency_5d,
    sf.trend_efficiency_10d,
    sf.trend_efficiency_20d,
    sf.trend_efficiency_60d,
    sf.ma5d_slope,
    sf.ma10d_slope,
    sf.ma20_slope,
    sf.ma50_slope,
    sf.recent_high_5d,
    sf.recent_low_5d,
    sf.recent_high_10d,
    sf.recent_low_10d,
    sf.recent_high_20d,
    sf.recent_low_20d,
    sf.range_position_5d,
    sf.range_position_10d,
    sf.range_position_20d,
    sf.relative_strength_vs_sector,
    sf.regime
FROM "UnderlyingSnapshot" us
LEFT JOIN "SignalFeatureDaily" sf
    ON sf.symbol = us.underlying
    AND sf.signal_date = us.trade_date
    AND sf.feature_version = 'v1'
WHERE us.underlying = %(underlying)s
  AND us.trade_date >= %(start_date)s
  AND us.trade_date <= %(end_date)s
ORDER BY us.trade_date
"""

_OPTIONS_QUERY = """
WITH ranked AS (
    SELECT
        os.id                          AS option_snapshot_id,
        oi.id                          AS option_instrument_id,
        oi.instrument_token,
        oi.underlying,
        oi.tradingsymbol,
        oi.strike,
        oi.expiry,
        oi.instrument_type,
        os.snapshot_time,
        CASE
            WHEN os.snapshot_label IS NULL
             AND os.snapshot_time::time = TIME '15:15:00' THEN 'CLOSE_1515'
            ELSE os.snapshot_label
        END                            AS snapshot_label,
        os.trade_date,
        os.data_source,
        us.close_price                 AS nifty_close_price,
        os.last_price                  AS option_price,
        os.bid_price,
        os.ask_price,
        os.volume,
        os.open_interest,
        osc.implied_volatility,
        osc.delta,
        osc.gamma,
        osc.theta,
        osc.vega,
        ROW_NUMBER() OVER (
            PARTITION BY
                oi.id,
                os.trade_date,
                CASE
                    WHEN os.snapshot_label IS NULL
                     AND os.snapshot_time::time = TIME '15:15:00' THEN 'CLOSE_1515'
                    ELSE os.snapshot_label
                END
            ORDER BY
                CASE os.data_source
                    WHEN 'KITE_QUOTE_LIVE'                  THEN 0
                    WHEN 'KITE_HISTORICAL_5M_CLOSE_PROXY'   THEN 1
                    ELSE 2
                END,
                os.snapshot_time DESC,
                os.id DESC
        ) AS rn
    FROM "OptionInstrument" oi
    JOIN "OptionSnapshot" os
        ON os.option_instrument_id = oi.id
    LEFT JOIN "OptionSnapshotCalc" osc
        ON osc.option_snapshot_id = os.id
    LEFT JOIN "UnderlyingSnapshot" us
        ON us.underlying = oi.underlying
       AND us.trade_date = os.trade_date
    WHERE oi.underlying = %(underlying)s
      AND os.trade_date >= %(start_date)s
      AND os.trade_date <= %(end_date)s
      AND os.data_source IN ('KITE_QUOTE_LIVE', 'KITE_HISTORICAL_5M_CLOSE_PROXY')
      AND (
          %(snapshot_label)s = 'all'
          OR (
              %(snapshot_label)s = 'close'
              AND (
                  UPPER(COALESCE(os.snapshot_label, '')) = 'CLOSE_1515'
                  OR (os.snapshot_label IS NULL AND os.snapshot_time::time = TIME '15:15:00')
              )
          )
          OR (
              %(snapshot_label)s = 'open'
              AND UPPER(COALESCE(os.snapshot_label, '')) = 'OPEN_0915'
          )
          OR LOWER(COALESCE(os.snapshot_label, '')) = %(snapshot_label)s
      )
)
SELECT
    option_snapshot_id,
    option_instrument_id,
    instrument_token,
    underlying,
    tradingsymbol,
    strike,
    expiry,
    instrument_type,
    snapshot_time,
    snapshot_label,
    trade_date,
    data_source,
    nifty_close_price,
    option_price,
    bid_price,
    ask_price,
    volume,
    open_interest,
    implied_volatility,
    delta,
    gamma,
    theta,
    vega
FROM ranked
WHERE rn = 1
ORDER BY trade_date, snapshot_label, expiry, strike, instrument_type, tradingsymbol
"""


def _clamp_option_start(start_date: date) -> date:
    if start_date < MIN_OPTION_START_DATE:
        print(f"[WARN] Option data starts {MIN_OPTION_START_DATE}; clamping options start from {start_date}.")
        return MIN_OPTION_START_DATE
    return start_date


def export_underlying(db, path: Path, underlying: str, start_date: date, end_date: date) -> int:
    df = pd.read_sql_query(
        _UNDERLYING_QUERY,
        db.conn,
        params={"underlying": underlying, "start_date": start_date, "end_date": end_date},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=underlying, index=False)
    print(f"Underlying: {len(df)} rows -> {path}")
    return len(df)


def export_options(
    db, path: Path, underlying: str, start_date: date, end_date: date, snapshot_label: str
) -> int:
    df = pd.read_sql_query(
        _OPTIONS_QUERY,
        db.conn,
        params={
            "underlying": underlying,
            "start_date": start_date,
            "end_date": end_date,
            "snapshot_label": snapshot_label.lower(),
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Options", index=False)
    print(f"Options:    {len(df)} rows -> {path}")
    return len(df)


def export_all(
    output_dir: Path,
    underlying: str,
    start_date: date,
    end_date: date,
    snapshot_label: str = "close",
) -> dict:
    option_start = _clamp_option_start(start_date)
    u_stem = f"{underlying}_{start_date}_{end_date}"
    o_stem = f"{underlying}_{option_start}_{end_date}"

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        u_rows = export_underlying(db, output_dir / f"{u_stem}_underlying.xlsx", underlying, start_date, end_date)
        o_rows = export_options(db, output_dir / f"{o_stem}_options.xlsx", underlying, option_start, end_date, snapshot_label)
    finally:
        db.close()

    return {
        "underlying": underlying,
        "underlying_start_date": start_date.isoformat(),
        "option_start_date": option_start.isoformat(),
        "end_date": end_date.isoformat(),
        "underlying_rows": u_rows,
        "option_rows": o_rows,
        "underlying_file": str(output_dir / f"{u_stem}_underlying.xlsx"),
        "options_file": str(output_dir / f"{o_stem}_options.xlsx"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export NIFTY underlying and option data to Excel.")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument(
        "--start",
        default=MIN_UNDERLYING_START_DATE.isoformat(),
        help=f"Start date YYYY-MM-DD for underlying. Options are clamped to {MIN_OPTION_START_DATE}. Default: {MIN_UNDERLYING_START_DATE}",
    )
    parser.add_argument(
        "--end",
        default=date.today().isoformat(),
        help="End date YYYY-MM-DD. Default: today",
    )
    parser.add_argument(
        "--snapshot-label",
        default="close",
        help="Option snapshot filter: close, open, or all. Default: close",
    )
    parser.add_argument(
        "--output-dir",
        default="output/db",
        help="Directory for output files. Default: output/db",
    )
    args = parser.parse_args()

    result = export_all(
        output_dir=Path(args.output_dir),
        underlying=args.underlying.upper(),
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        snapshot_label=args.snapshot_label,
    )
    print(result)


if __name__ == "__main__":
    main()
