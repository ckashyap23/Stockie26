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
from src.common.config import normalize_pct


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
    }


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

    option_result = run_daily_option_selection(
        underlying=underlying,
        trade_date=trade_date,
        model_version=model_version,
        target_pcts=target_pcts,
        stop_loss_pct=stop_loss_pct,
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
