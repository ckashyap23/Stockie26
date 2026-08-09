"""
Cron-friendly NIFTY signal job.

Assumes the upstream daily market refresh, news sentiment, global index fetch,
option instrument refresh, option snapshot, and option calc jobs have already run.

It runs the production NIFTY prediction, then runs option selection for the latest
prediction row unless --trade-date is supplied. The selected option and trade-plan
levels are persisted to NiftyOptionSelection and printed as JSON for cron logs.

Usage:
    python scripts/daily_NIFTY/daily_nifty_signal.py
    python scripts/daily_NIFTY/daily_nifty_signal.py --trade-date 2026-06-25
    python scripts/daily_NIFTY/daily_nifty_signal.py --skip-prediction --trade-date 2026-06-25
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from scripts.daily_NIFTY.daily_nifty_prediction import run_daily_nifty_prediction
from scripts.daily_NIFTY.daily_option_selection import run_daily_option_selection
from src.common.config import get_settings, normalize_pct, get_paper_capital_per_trade_pct
from src.data_manager.db.supabase_client import SupabaseDatabaseClient
from src.technical_analysis.cascade.drift_overrule import apply_drift_overrule, load_drift_inputs


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _signal_payload(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": selection.get("symbol", "NIFTY"),
        "trade_date": selection.get("trade_date"),
        "model_version": selection.get("model_version"),
        "prediction": selection.get("prediction_direction"),
        "strength_score": selection.get("strength_score"),
        "selected_strategy": selection.get("selected_strategy"),
        "no_trade_reason": selection.get("no_trade_reason"),
        "primary_buy_token": selection.get("primary_buy_token"),
        "primary_buy_symbol": selection.get("primary_buy_symbol"),
        "primary_buy_strike": selection.get("primary_buy_strike"),
        "primary_buy_expiry": selection.get("primary_buy_expiry"),
        "primary_buy_option_type": selection.get("primary_buy_option_type"),
        "entry_reference_price": selection.get("primary_buy_entry_price"),
        "target_pct": selection.get("target_1_pct"),
        "target_price": selection.get("target_1_price"),
        "stop_loss_enabled": selection.get("stop_loss_enabled"),
        "stop_loss_pct": selection.get("stop_loss_pct"),
        "stop_loss_price": selection.get("stop_loss_price"),
        "drift_overrule_reason": selection.get("drift_overrule_reason"),
        "drift_position_size_pct": selection.get("drift_position_size_pct"),
    }


def _apply_and_store_drift_overrule(
    underlying: str,
    model_version: str,
    trade_date: str | None = None,
) -> tuple[str | None, float | None, str | None]:
    """Compute and persist drift_effective_prediction for the latest (or specified)
    signal_date. Historical rows are never touched — upsert_drift_overrule issues a
    keyed UPDATE so only the matching row is written.
    Returns (drift_effective_prediction, drift_position_size_pct, drift_overrule_reason).
    """
    settings = get_settings()
    db = SupabaseDatabaseClient(settings)
    db.connect()
    try:
        with db.conn.cursor() as cur:
            if trade_date:
                cur.execute(
                    'SELECT symbol, signal_date, model_version, effective_prediction,'
                    '  watch_signal, promoted_prediction, vix_chg_1d,'
                    '  global_asia_overnight_return_mean,'
                    '  event_gate_reason, promotion_block_reason'
                    ' FROM "NiftyPrediction"'
                    ' WHERE UPPER(symbol)=%s AND model_version=%s AND signal_date=%s LIMIT 1',
                    (underlying.upper(), model_version, trade_date),
                )
            else:
                cur.execute(
                    'SELECT symbol, signal_date, model_version, effective_prediction,'
                    '  watch_signal, promoted_prediction, vix_chg_1d,'
                    '  global_asia_overnight_return_mean,'
                    '  event_gate_reason, promotion_block_reason'
                    ' FROM "NiftyPrediction"'
                    ' WHERE UPPER(symbol)=%s AND model_version=%s'
                    ' ORDER BY signal_date DESC LIMIT 1',
                    (underlying.upper(), model_version),
                )
            pred_row = cur.fetchone()
            if not pred_row:
                return None, None, None
            pred = dict(zip([d[0] for d in cur.description], pred_row))

        signal_date = pred["signal_date"]

        with db.conn.cursor() as cur:
            cur.execute(
                'SELECT nifty_gap_pct, nifty_drift_pct, gap_open_atr'
                ' FROM "SignalFeatureDaily"'
                ' WHERE symbol=%s AND signal_date=%s',
                (underlying.upper(), signal_date),
            )
            gap_row = cur.fetchone()

        gap_features = {}
        if gap_row:
            gap_features = {
                "nifty_gap_pct":   gap_row[0],
                "nifty_drift_pct": gap_row[1],
                "gap_open_atr":    gap_row[2],
            }

        inputs = load_drift_inputs(pred, gap_features, get_paper_capital_per_trade_pct())
        result = apply_drift_overrule(inputs)

        print(
            f"  Drift overrule [{signal_date}]: {pred['effective_prediction']}"
            f" -> {result.drift_effective_prediction}"
            f" ({result.drift_overrule_reason}) size={result.drift_position_size_pct}"
        )

        # upsert_drift_overrule issues a keyed UPDATE — only this signal_date is touched.
        db.upsert_drift_overrule([{
            "symbol":                     underlying.upper(),
            "signal_date":                signal_date,
            "model_version":              model_version,
            "drift_effective_prediction": result.drift_effective_prediction,
            "drift_position_size_pct":    result.drift_position_size_pct,
            "drift_overrule_reason":      result.drift_overrule_reason,
        }])
        return result.drift_effective_prediction, result.drift_position_size_pct, result.drift_overrule_reason
    finally:
        db.close()


def run_daily_nifty_signal(
    underlying: str = "NIFTY",
    trade_date: str | None = None,
    model_version: str = "cascade_v1",
    target_pcts: tuple[float, ...] | None = None,
    stop_loss_pct: float | None = None,
    skip_prediction: bool = False,
) -> dict[str, Any]:
    prediction_result: dict[str, Any] | None = None
    if not skip_prediction:
        prediction_result = run_daily_nifty_prediction(
            underlying=underlying,
            model_version=model_version,
        )

    # ── Drift overrule ─────────────────────────────────────────────────────────────
    # Computes and stores drift for today's signal date (D) only.
    # Historical rows are never touched: upsert_drift_overrule issues a keyed
    # UPDATE on (symbol, signal_date, model_version) and upsert_nifty_predictions
    # excludes drift columns from its ON CONFLICT UPDATE SET (_drift_never_update).
    drift_direction, drift_size, drift_reason = _apply_and_store_drift_overrule(
        underlying=underlying,
        model_version=model_version,
        trade_date=trade_date,
    )

    option_result = run_daily_option_selection(
        underlying=underlying,
        trade_date=trade_date,
        model_version=model_version,
        target_pcts=target_pcts,
        stop_loss_pct=stop_loss_pct,
        direction_override=drift_direction,
        position_size_override=drift_size,
    )
    payload = {
        "prediction_rows": prediction_result.get("db_rows") if prediction_result else None,
        "option_selection_rows": option_result["rows"],
        "signal": _signal_payload(option_result["selection"]),
    }
    print("FINAL_SIGNAL_JSON=" + json.dumps(payload, default=_json_default, sort_keys=True))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run NIFTY prediction and option selection for cron, then print one selected option trade plan."
    )
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--trade-date", default=None, help="Signal trade_date. Default: latest prediction row")
    parser.add_argument("--model-version", default="cascade_v1", help="Model version. Default: cascade_v1")
    parser.add_argument("--skip-prediction", action="store_true", help="Only run option selection against existing NiftyPrediction rows.")
    parser.add_argument(
        "--target-pct",
        action="append",
        type=float,
        default=None,
        help="Option profit target. Default: regime target from *_TARGET_PCT. Use decimal values such as 0.05 or whole-percent values such as 5.",
    )
    parser.add_argument(
        "--stop-loss-pct",
        type=float,
        default=None,
        help="Optional option stop-loss. Use decimal values such as 0.05 or whole-percent values such as 5. Omit to disable stop loss for option selection.",
    )
    args = parser.parse_args()
    target_pcts = (normalize_pct(args.target_pct[0]),) if args.target_pct else None
    stop_loss_pct = normalize_pct(args.stop_loss_pct) if args.stop_loss_pct is not None else None

    run_daily_nifty_signal(
        underlying=args.underlying.upper(),
        trade_date=args.trade_date,
        model_version=args.model_version,
        target_pcts=target_pcts,  # type: ignore[arg-type]
        stop_loss_pct=stop_loss_pct,
        skip_prediction=args.skip_prediction,
    )


if __name__ == "__main__":
    main()
