"""
Daily signal summary: prints prediction direction and selected option instrument.

Reads NiftyPrediction (effective_prediction) and NiftyOptionSelection
(primary_buy_symbol) for the given execution date and outputs a one-line summary
suitable for cron logging or downstream piping.

Usage:
    python scripts/daily_NIFTY/daily_signal_summary.py
    python scripts/daily_NIFTY/daily_signal_summary.py --trade-date 2026-08-11
    python scripts/daily_NIFTY/daily_signal_summary.py --underlying NIFTY --model-version cascade_v1
    python scripts/daily_NIFTY/daily_signal_summary.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client


def _default_trade_date() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def fetch_signal_summary(
    conn,
    symbol: str,
    model_version: str,
    trade_date: date,
) -> dict:
    """
    Join NiftyPrediction + NiftyOptionSelection for the given execution date.

    NiftyPrediction.next_trade_date == trade_date (execution/paper-trade date).
    NiftyOptionSelection.next_trade_date == trade_date (same key).
    """
    sql = """
        SELECT
            p.signal_date,
            p.next_trade_date,
            COALESCE(p.effective_prediction, 'NO_POSITION') AS direction,
            p.strength_score,
            p.volatility_regime,
            o.selected_strategy,
            o.primary_buy_symbol  AS option_instrument,
            o.primary_buy_strike  AS strike,
            o.primary_buy_expiry  AS expiry,
            o.primary_buy_option_type AS option_type,
            o.primary_buy_entry_price AS entry_ref_price,
            o.selection_score,
            o.no_trade_reason
        FROM "NiftyPrediction" p
        LEFT JOIN "NiftyOptionSelection" o
               ON o.symbol        = p.symbol
              AND o.model_version = p.model_version
              AND o.trade_date    = p.signal_date
        WHERE UPPER(p.symbol)       = %s
          AND p.model_version       = %s
          AND p.next_trade_date     = %s
        ORDER BY p.signal_date DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (symbol.upper(), model_version, trade_date))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        return {}
    return dict(zip(cols, row, strict=False))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print today's prediction direction and option instrument."
    )
    parser.add_argument("--trade-date", default=None, help="Execution date YYYY-MM-DD. Default: today IST")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--model-version", default="cascade_v1", help="Model version. Default: cascade_v1")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON instead of plain text")
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date) if args.trade_date else _default_trade_date()
    symbol = args.underlying.upper()

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        summary = fetch_signal_summary(db.conn, symbol, args.model_version, trade_date)
    finally:
        db.close()

    if not summary:
        result = {
            "trade_date": trade_date.isoformat(),
            "underlying": symbol,
            "direction": "NO_POSITION",
            "option_instrument": None,
            "reason": "No NiftyPrediction row found for this execution date",
        }
    else:
        result = {
            "trade_date": trade_date.isoformat(),
            "signal_date": str(summary.get("signal_date") or ""),
            "underlying": symbol,
            "direction": summary.get("direction") or "NO_POSITION",
            "strength_score": summary.get("strength_score"),
            "volatility_regime": summary.get("volatility_regime"),
            "selected_strategy": summary.get("selected_strategy"),
            "option_instrument": summary.get("option_instrument"),
            "strike": summary.get("strike"),
            "expiry": str(summary.get("expiry") or ""),
            "option_type": summary.get("option_type"),
            "entry_ref_price": summary.get("entry_ref_price"),
            "selection_score": summary.get("selection_score"),
            "no_trade_reason": summary.get("no_trade_reason"),
        }

    if args.as_json:
        print(json.dumps(result, default=str))
    else:
        direction = result["direction"]
        instrument = result.get("option_instrument") or "—"
        reason = result.get("no_trade_reason") or ""
        print(
            f"{trade_date}  {symbol}  direction={direction}"
            + (f"  instrument={instrument}" if direction != "NO_POSITION" else f"  no_trade_reason={reason}")
        )
        if direction != "NO_POSITION" and result.get("strike"):
            print(
                f"  strike={result['strike']}  expiry={result['expiry']}"
                f"  entry_ref={result['entry_ref_price']}  score={result['selection_score']}"
            )


if __name__ == "__main__":
    main()
