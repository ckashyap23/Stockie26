"""Backfill drift_effective_prediction, drift_position_size_pct, drift_overrule_reason
onto existing NiftyPrediction rows for a date range.

For each signal_date in range:
  1. Reads the NiftyPrediction row (effective_prediction, watch_signal, etc.)
  2. Reads gap features from SignalFeatureDaily (nifty_gap_pct, nifty_drift_pct, gap_open_atr)
  3. Applies apply_drift_overrule()
  4. Upserts the three drift columns via upsert_drift_overrule()

Usage:
    python scripts/backfill_NIFTY/backfill_drift_overrule.py --start 2024-01-01
    python scripts/backfill_NIFTY/backfill_drift_overrule.py --start 2024-01-01 --end 2026-07-23
    python scripts/backfill_NIFTY/backfill_drift_overrule.py --start 2024-01-01 --model-version cascade_v1
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings, get_paper_capital_per_trade_pct
from src.data_manager.db.supabase_client import SupabaseDatabaseClient
from src.technical_analysis.cascade.drift_overrule import apply_drift_overrule, load_drift_inputs


def run_backfill_drift_overrule(
    start_date: date,
    end_date: date,
    underlying: str = "NIFTY",
    model_version: str = "cascade_v1",
) -> dict:
    settings = get_settings()
    db = SupabaseDatabaseClient(settings)
    db.connect()
    try:
        # 1) Load all NiftyPrediction rows in range
        with db.conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, signal_date, model_version,
                       effective_prediction, watch_signal, promoted_prediction,
                       vix_chg_1d, global_asia_overnight_return_mean,
                       event_gate_reason, promotion_block_reason
                FROM "NiftyPrediction"
                WHERE UPPER(symbol) = %s
                  AND model_version = %s
                  AND signal_date BETWEEN %s AND %s
                ORDER BY signal_date
                """,
                (underlying.upper(), model_version, start_date, end_date),
            )
            cols = [d[0] for d in cur.description]
            pred_rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        if not pred_rows:
            print(f"No NiftyPrediction rows found for {start_date} .. {end_date}")
            return {"processed": 0, "skipped": 0}

        # 2) Load gap features for all dates in one query
        signal_dates = [r["signal_date"] for r in pred_rows]
        with db.conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, signal_date,
                       nifty_gap_pct, nifty_drift_pct, gap_open_atr
                FROM "SignalFeatureDaily"
                WHERE UPPER(symbol) = %s
                  AND signal_date = ANY(%s)
                """,
                (underlying.upper(), signal_dates),
            )
            gap_cols = [d[0] for d in cur.description]
            gap_map: dict[date, dict] = {
                row[1]: dict(zip(gap_cols, row))
                for row in cur.fetchall()
            }

        base_pct = get_paper_capital_per_trade_pct()

        # 3) Apply drift overrule for each row and collect results
        drift_rows = []
        skipped = 0
        for pred in pred_rows:
            sig_date = pred["signal_date"]
            gap_features = gap_map.get(sig_date, {})
            if not gap_features:
                skipped += 1
            inputs = load_drift_inputs(pred, gap_features, base_pct)
            result = apply_drift_overrule(inputs)
            drift_rows.append({
                "symbol":                     underlying.upper(),
                "signal_date":                sig_date,
                "model_version":              model_version,
                "drift_effective_prediction": result.drift_effective_prediction,
                "drift_position_size_pct":    result.drift_position_size_pct,
                "drift_overrule_reason":      result.drift_overrule_reason,
            })
            print(
                f"  {sig_date}  {pred['effective_prediction']:>12} -> "
                f"{result.drift_effective_prediction:>12}  "
                f"({result.drift_overrule_reason})"
                + ("  [no gap features]" if not gap_features else "")
            )

        # 4) Bulk upsert
        db.upsert_drift_overrule(drift_rows)
        print(
            f"\nDrift backfill complete: {len(drift_rows)} rows updated, "
            f"{skipped} had no gap features (NO_CHANGE applied)."
        )
        return {"processed": len(drift_rows), "skipped": skipped}

    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill drift overrule columns onto NiftyPrediction rows."
    )
    parser.add_argument("--start", required=True, help="Start signal_date (YYYY-MM-DD).")
    parser.add_argument("--end", default=None, help="End signal_date (YYYY-MM-DD). Default: today.")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--model-version", default="cascade_v1")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()

    print(f"Backfilling drift overrule: {args.underlying} {args.model_version} "
          f"{start} .. {end}")
    run_backfill_drift_overrule(
        start_date=start,
        end_date=end,
        underlying=args.underlying,
        model_version=args.model_version,
    )


if __name__ == "__main__":
    main()
