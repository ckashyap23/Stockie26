"""
Batch-upsert NiftyOptionSelection for all CALL/PUT signal dates in NiftyPrediction.

This is Step 2 of the production backtest pipeline:
  1. scripts/daily_NIFTY/daily_nifty_prediction.py        -- upserts NiftyPrediction
  2. backtest/production/pipeline_upsert_option_selections.py  -- upserts NiftyOptionSelection  ← this script
  3. backtest/production/pipeline_backtest_pnl.py          -- simulates PnL

Pre-April 2026 dates produce NO_TRADE (no OptionInstrument/OptionSnapshot data).
April 2026+ dates get live option selections with regime-aware target/SL pcts from .env.

Usage:
    python backtest/production/pipeline_upsert_option_selections.py
    python backtest/production/pipeline_upsert_option_selections.py --underlying NIFTY --model-version cascade_v1
    python backtest/production/pipeline_upsert_option_selections.py --start 2026-04-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.technical_analysis.optionselection.pipeline import run_option_selection_from_db


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-upsert NiftyOptionSelection for all CALL/PUT signal dates."
    )
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--model-version", default="cascade_v1")
    parser.add_argument("--start", default=None, help="Only process signal_dates >= this date (YYYY-MM-DD).")
    parser.add_argument("--end", default=None, help="Only process signal_dates <= this date (YYYY-MM-DD).")
    args = parser.parse_args()

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()

    params: list = [args.underlying.upper(), args.model_version]
    date_filter = ""
    if args.start:
        date_filter += " AND signal_date >= %s"
        params.append(args.start)
    if args.end:
        date_filter += " AND signal_date <= %s"
        params.append(args.end)

    with db.conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT signal_date,
                   effective_prediction,
                   drift_effective_prediction,
                   drift_position_size_pct
            FROM "NiftyPrediction"
            WHERE symbol = %s AND model_version = %s
              AND (
                  -- drift has been applied: use its result
                  (drift_effective_prediction IS NOT NULL AND drift_effective_prediction IN ('CALL', 'PUT'))
                  OR
                  -- drift not yet applied: fall back to cascade effective_prediction
                  (drift_effective_prediction IS NULL AND effective_prediction IN ('CALL', 'PUT'))
              )
              {date_filter}
            ORDER BY signal_date
            """,
            params,
        )
        signal_rows = [
            {"signal_date": str(r[0]), "effective_prediction": r[1],
             "drift_effective_prediction": r[2], "drift_position_size_pct": r[3]}
            for r in cur.fetchall()
        ]
    db.close()

    if not signal_rows:
        print("No CALL/PUT signal dates found.")
        return

    dates_summary = [r["signal_date"] for r in signal_rows]
    print(f"Signal dates to process: {len(dates_summary)}  ({dates_summary[0]} .. {dates_summary[-1]})")
    ok = skipped = 0
    for row in signal_rows:
        trade_date = row["signal_date"]
        # Final direction: drift takes priority when set
        final_direction = row["drift_effective_prediction"] or row["effective_prediction"]
        position_size = row["drift_position_size_pct"]
        db2 = get_database_client(settings)
        db2.connect()
        try:
            result = run_option_selection_from_db(
                db2,
                underlying=args.underlying.upper(),
                trade_date=trade_date,
                model_version=args.model_version,
                direction_override=final_direction,
                position_size_override=position_size,
            )
            sel = result["selection"]
            strategy = sel.get("selected_strategy", "n/a")
            symbol = sel.get("primary_buy_symbol") or "-"
            t1 = sel.get("target_1_price") or "-"
            sl = sel.get("stop_loss_price") or "disabled"
            print(f"  {trade_date}  {strategy:<20}  {symbol:<28}  t1={t1}  sl={sl}")
            ok += 1
        except Exception as exc:
            print(f"  {trade_date}  SKIPPED: {exc}")
            skipped += 1
        finally:
            db2.close()

    print(f"\nDone: {ok} upserted, {skipped} skipped/no-data")


if __name__ == "__main__":
    main()
