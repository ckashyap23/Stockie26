"""
Daily NIFTY option-selection job.

Reads the upstream production prediction from Supabase "NiftyPrediction", reads
option chain snapshots/Greeks from Supabase, selects the option strategy using the
current production option selector, and persists the result to
"NiftyOptionSelection".

Usage:
    python scripts/daily_NIFTY/daily_option_selection.py --trade-date 2026-06-24
    python scripts/daily_NIFTY/daily_option_selection.py --model-version cascade_v1
    python scripts/daily_NIFTY/daily_option_selection.py --target-pct 0.03 --target-pct 0.05 --stop-loss-pct 0.05
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings, normalize_pct
from src.data_manager.db.client_factory import get_database_client
from src.technical_analysis.optionselection.pipeline import run_option_selection_from_db


def run_daily_option_selection(
    underlying: str = "NIFTY",
    trade_date: str | None = None,
    model_version: str = "cascade_v1",
    target_pcts: tuple[float, float] | None = None,
    stop_loss_pct: float | None = None,
) -> dict:
    settings = get_settings()
    if not (settings.database_provider == "supabase" or settings.supabase_conn_str):
        raise RuntimeError(
            "daily_option_selection.py reads/writes Supabase only; "
            "set DATABASE_PROVIDER=supabase and SUPABASE_CONN_STR."
        )
    db = get_database_client(settings)
    db.connect()
    try:
        result = run_option_selection_from_db(
            db,
            underlying=underlying.upper(),
            trade_date=trade_date,
            model_version=model_version,
            target_pcts=target_pcts,
            stop_loss_pct=stop_loss_pct,
        )
    finally:
        db.close()

    selection = result["selection"]
    print(
        f"option selection: {selection['trade_date']} "
        f"{selection['prediction_direction']} strength={selection.get('strength_score')} "
        f"-> {selection['selected_strategy']}"
        + (f" ({selection['primary_buy_symbol']} token={selection['primary_buy_token']})" if selection.get("primary_buy_symbol") else "")
    )
    if selection.get("primary_buy_token"):
        print(
            "trade plan: "
            f"entry_ref={selection.get('primary_buy_entry_price')} "
            f"target={selection.get('target_1_price')} "
            f"stop_loss={'disabled' if not selection.get('stop_loss_enabled') else selection.get('stop_loss_price')}"
        )
    print(f"Upserted {result['rows']} row(s) into NiftyOptionSelection "
          f"(model_version={model_version}).")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily NIFTY option selection from Supabase NiftyPrediction to NiftyOptionSelection."
    )
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--trade-date", default=None, help="Signal trade_date OR execution next_trade_date. Default: latest prediction row")
    parser.add_argument("--model-version", default="cascade_v1", help="NiftyPrediction model_version. Default: cascade_v1")
    parser.add_argument(
        "--target-pct",
        action="append",
        type=float,
        default=None,
        help="Option profit target. Default: configured target from TARGET_PCT_EFFECTIVE/TARGET_PCT. Use decimal values such as 0.05 or whole-percent values such as 5.",
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

    result = run_daily_option_selection(
        underlying=args.underlying.upper(),
        trade_date=args.trade_date,
        model_version=args.model_version,
        target_pcts=target_pcts,  # type: ignore[arg-type]
        stop_loss_pct=stop_loss_pct,
    )
    selection = result["selection"]
    print({
        "rows": result["rows"],
        "trade_date": selection["trade_date"],
        "primary_buy_token": selection.get("primary_buy_token"),
        "primary_buy_symbol": selection.get("primary_buy_symbol"),
        "target_1_price": selection.get("target_1_price"),
        "stop_loss_price": selection.get("stop_loss_price"),
    })


if __name__ == "__main__":
    main()

