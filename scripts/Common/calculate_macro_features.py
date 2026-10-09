"""Compute macro regime features and persist them to SignalFeatureDaily."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_macro_trailing_window, get_settings
from src.data_manager.db.client_factory import get_database_client
from src.technical_analysis.macro_features import (
    MACRO_FEATURE_COLUMNS,
    calculate_macro_features,
)


SIGNAL_SCHEMA_SQL = """
ALTER TABLE "SignalFeatureDaily"
    ADD COLUMN IF NOT EXISTS crude_ret_1d double precision,
    ADD COLUMN IF NOT EXISTS crude_ret_5d double precision,
    ADD COLUMN IF NOT EXISTS us10y_chg_1d double precision,
    ADD COLUMN IF NOT EXISTS us10y_chg_5d double precision,
    ADD COLUMN IF NOT EXISTS us10y_level double precision,
    ADD COLUMN IF NOT EXISTS usdinr_ret_1d double precision,
    ADD COLUMN IF NOT EXISTS usdinr_ret_5d double precision,
    ADD COLUMN IF NOT EXISTS fii_net_cr double precision,
    ADD COLUMN IF NOT EXISTS fii_net_1d_cr double precision,
    ADD COLUMN IF NOT EXISTS fii_net_5d_cr double precision,
    ADD COLUMN IF NOT EXISTS dii_net_cr double precision,
    ADD COLUMN IF NOT EXISTS fii_idx_long_ratio double precision,
    ADD COLUMN IF NOT EXISTS fii_idx_long_ratio_chg5 double precision,
    ADD COLUMN IF NOT EXISTS crude_ret_5d_z double precision,
    ADD COLUMN IF NOT EXISTS us10y_chg_5d_z double precision,
    ADD COLUMN IF NOT EXISTS usdinr_ret_5d_z double precision,
    ADD COLUMN IF NOT EXISTS fii_net_5d_z double precision,
    ADD COLUMN IF NOT EXISTS fii_idx_5d_z double precision,
    ADD COLUMN IF NOT EXISTS macro_risk_off double precision;
"""


def run_calculate_macro_features(start_date: date, end_date: date) -> dict:
    window = get_macro_trailing_window()
    settings = get_settings()
    db = get_database_client(settings)
    if getattr(db, "db_kind", "") != "postgres":
        raise RuntimeError("Macro feature calculation currently supports Supabase/Postgres only")
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                """
                SELECT factor_date, crude_close, us10y_level, usdinr_close,
                       fii_net_cr, dii_net_cr, fii_idx_fut_long,
                       fii_idx_fut_short
                FROM "MacroFactorDaily"
                WHERE factor_date <= %s
                ORDER BY factor_date
                """,
                (end_date,),
            )
            rows = cur.fetchall()
            columns = [description[0] for description in cur.description]
        raw = pd.DataFrame(rows, columns=columns)
        features = calculate_macro_features(raw, trailing_window=window)
        target = features[
            pd.Series(features["signal_date"]).between(start_date, end_date, inclusive="both")
        ]

        with db.conn.cursor() as cur:
            cur.execute(SIGNAL_SCHEMA_SQL)
            if not target.empty:
                from psycopg2.extras import execute_values

                update_clause = ", ".join(
                    f"{column} = data.{column}::double precision"
                    for column in MACRO_FEATURE_COLUMNS
                )
                value_columns = ["signal_date", *MACRO_FEATURE_COLUMNS]
                updated = execute_values(
                    cur,
                    f"""
                    UPDATE "SignalFeatureDaily" AS sfd
                    SET {update_clause}, updated_at = now()
                    FROM (VALUES %s) AS data ({", ".join(value_columns)})
                    WHERE sfd.signal_date = data.signal_date::date
                      AND sfd.symbol = 'NIFTY'
                    RETURNING sfd.signal_date
                    """,
                    [
                        tuple(None if pd.isna(record[column]) else record[column] for column in value_columns)
                        for record in target.to_dict("records")
                    ],
                    fetch=True,
                )
            else:
                updated = []
        db.conn.commit()
    finally:
        db.close()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "trailing_window": window,
        "rows_updated": len(updated),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate and persist NIFTY macro features.")
    parser.add_argument("--start", required=True, help="Signal start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Signal end date YYYY-MM-DD")
    args = parser.parse_args()
    print(
        run_calculate_macro_features(
            date.fromisoformat(args.start),
            date.fromisoformat(args.end),
        )
    )


if __name__ == "__main__":
    main()
