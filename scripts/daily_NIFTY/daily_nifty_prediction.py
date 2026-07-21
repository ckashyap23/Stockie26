"""
Daily NIFTY final-prediction job (regime-aware precision cascade).

This is the PRODUCTION daily entrypoint that predicts the next trading day (n+1)
and persists the result. It is prediction-only: it assumes the upstream daily
market refresh has already run for the day, i.e. "SignalFeatureDaily" (prices +
features), the NIFTY futures volume and India VIX in "MacroFactorDaily" are
up to date. Run scripts/daily_NIFTY/daily_market_refresh.py first.

What it does
------------
  1. Runs the shared regime-aware cascade via
     src.technical_analysis.cascade.pipeline.generate_prediction_csv, which drives
     the shared cascade engine (src/technical_analysis/cascade) with the PROMOTED
     strategy roster â€” the same engine the research harness
     (backtest/vectorbt_research/strategy_grid.py) drives with the full roster, so the
     engine never drifts between research and production. This writes:
       - output/backtest/NIFTY/production/NIFTY_prediction.csv
       - output/backtest/NIFTY/production/NIFTY_prediction_summary.txt
     The latest unresolved day (n+1) is predicted; its actual_trade_label stays
     blank until the outcome lands on a later run.
  2. Upserts every prediction row into the Supabase "NiftyPrediction" table
     (durable across Render runs; the CSV/summary remain for the Flask dashboard).

Why both CSV and DB: Render's filesystem is ephemeral, so the DB is the durable
record (and lets yesterday's pending row get its actual_trade_label filled in on
the next run); the CSV/summary are kept for the local dashboard.

Usage:
    python scripts/daily_NIFTY/daily_nifty_prediction.py
    python scripts/daily_NIFTY/daily_nifty_prediction.py --model-version cascade_v1
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.technical_analysis.cascade.pipeline import DEFAULT_OUTPUT, generate_prediction_csv
from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client

# Columns persisted to "NiftyPrediction" (everything the cascade CSV exposes).
_DB_COLS = [
    "signal_date", "next_trade_date",
    "open_915", "high_day", "low_day", "close_1515", "volume_day",
    "vix_close", "vix_chg_1d", "vix_chg_pct", "regime",
    "next_open", "next_high", "next_low", "next_close", "next_return_pct",
    "final_prediction", "watch_signal", "prior_watch_signal", "prior_watch_age",
    "promoted_prediction", "effective_prediction", "promotion_reason",
    "direction", "volatility_regime", "primary_strategy",
    "primary_strategy_family", "primary_strategy_type",
    "strategy_precision", "signal_style", "strength_score", "strength_label",
    "confidence_level", "actual_trade_label",
    "bull_score", "bear_score", "signal_quality", "actual_quality_label",
    "quality_horizon_days",
    "global_risk_off",
    "global_gate_reason",
    "global_us_return_mean",
    "global_europe_return_mean",
    "global_asia_partial_return_mean",
    "global_asia_overnight_return_mean",
    "watch_family", "watch_variant", "watch_strategy_type",
    "prior_watch_family", "prior_watch_variant", "prior_watch_strategy_type",
    "confirming_family", "confirming_variant", "confirming_strategy_type",
    "family_confirmation_match",
    "promotion_block_reason",
    "position_size_pct",
]


def _frame_to_rows(df: pd.DataFrame, symbol: str, model_version: str) -> list[dict]:
    """Convert the prediction frame to upsert dicts, mapping NaN/NaT -> None so
    pending (n+1) rows store NULL outcomes.

    next_trade_date is now filled from TradingCalendar in dataset.py, so NaT only
    occurs when the calendar table is not populated far enough ahead. Such rows are
    skipped here as the execution date is genuinely unknown.
    """
    sub = df.reindex(columns=_DB_COLS)
    sub = sub.astype(object).where(pd.notna(sub), None)
    # Skip rows with no next_trade_date — calendar not populated far enough
    sub = sub[sub["next_trade_date"].notna()]
    rows: list[dict] = []
    for rec in sub.to_dict("records"):
        rec["symbol"] = symbol.upper()
        rec["model_version"] = model_version
        # global_risk_off is stored as 0.0/1.0 float in the frame; cast to Python bool
        if rec.get("global_risk_off") is not None:
            rec["global_risk_off"] = bool(rec["global_risk_off"])
        rows.append(rec)
    return rows


def run_daily_nifty_prediction(
    underlying: str = "NIFTY",
    output_path: Path = DEFAULT_OUTPUT,
    write_db: bool = True,
    model_version: str = "cascade_v1",
    start: str | None = None,
    end: str | None = None,
) -> dict:
    # 1) cascade → CSV + summary (also returns the prediction frame).
    result = generate_prediction_csv(
        underlying=underlying.upper(),
        output_path=output_path,
        start=start,
        end=end,
    )
    df = result.get("frame")
    if df is None or df.empty:
        print("No prediction rows produced; nothing to persist.")
        return {**{k: v for k, v in result.items() if k != "frame"}, "db_rows": 0}

    latest = df.iloc[-1]
    print(f"  latest prediction: {latest['signal_date']} "
          f"regime={latest['regime']} final={latest['final_prediction']} "
          f"effective={latest['effective_prediction']}"
          + (" (pending outcome)" if pd.isna(latest.get("actual_trade_label")) else ""))

    # 2) persist to Supabase (durable record across Render runs).
    db_rows = 0
    if write_db:
        settings = get_settings()
        if not (settings.database_provider == "supabase" or settings.supabase_conn_str):
            raise RuntimeError(
                "daily_nifty_prediction.py persists to Supabase only; "
                "set the Supabase provider or pass --no-db."
            )
        rows = _frame_to_rows(df, underlying, model_version)
        db = get_database_client(settings)
        db.connect()
        try:
            db_rows = db.upsert_nifty_predictions(rows)
        finally:
            db.close()
        print(f"Upserted {db_rows} row(s) into NiftyPrediction "
              f"(model_version={model_version}).")

    return {
        "rows": int(result.get("rows", len(df))),
        "graded_rows": int(result.get("graded_rows", 0)),
        "pending_predicted": int(result.get("pending_predicted", 0)),
        "db_rows": db_rows,
        "path": str(output_path),
        "summary_path": result.get("summary_path"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily NIFTY n+1 prediction via the regime-aware cascade; "
                    "writes CSV/summary and upserts to Supabase NiftyPrediction.")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"Output CSV path. Default: {DEFAULT_OUTPUT}")
    parser.add_argument("--model-version", default="cascade_v1",
                        help="Stored with each prediction row. Default: cascade_v1")
    parser.add_argument("--start", default=None, help="Only write/upsert signal_date >= this date (YYYY-MM-DD).")
    parser.add_argument("--end", default=None, help="Only write/upsert signal_date <= this date (YYYY-MM-DD).")
    args = parser.parse_args()

    result = run_daily_nifty_prediction(
        underlying=args.underlying.upper(),
        output_path=Path(args.output),
        write_db=True,
        model_version=args.model_version,
        start=args.start,
        end=args.end,
    )
    print(result)


if __name__ == "__main__":
    main()
