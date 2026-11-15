from __future__ import annotations

from technical_analysis_service.runtime import SERVICE_ROOT, output_dir as service_output_dir, load_environment

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

project_root = SERVICE_ROOT
load_environment()

from technical_analysis_service.backtest.vectorbt_trades.schemas import StockieVectorBTRequest
from technical_analysis_service.backtest.vectorbt_trades.service import run_stockie_vectorbt_backtest


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay actual executed paper/live trades through VectorBT for portfolio analytics. "
            "Reads from PaperTradeResult (actual fills), not from pipeline signal tables."
        )
    )
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--model-version", default="cascade_v1", help="Prediction model version. Default: cascade_v1")
    parser.add_argument(
        "--mode",
        default=os.getenv("MODE", "paper"),
        choices=("paper", "live"),
        help="Execution mode. Reads MODE env variable; defaults to 'paper'.",
    )
    parser.add_argument("--start", default=None, help="Start paper_trade_date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End paper_trade_date YYYY-MM-DD")
    parser.add_argument("--initial-cash", type=float, default=100_000.0,
                        help="Initial portfolio cash for vectorbt normalized metrics. Default: 100000")
    parser.add_argument("--fees", type=float, default=0.0,
                        help="Per-side fee fraction. Example: 0.0003")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="Per-side slippage fraction applied on top of actual fills. Example: 0.0005")
    parser.add_argument(
        "--output-dir",
        default=str(service_output_dir() / "backtest" / "NIFTY" / "vectorbt"),
        help="Output directory. Default: output/backtest/NIFTY/vectorbt",
    )
    args = parser.parse_args()

    request = StockieVectorBTRequest(
        underlying=args.underlying.upper(),
        model_version=args.model_version,
        mode=args.mode,
        start_date=date.fromisoformat(args.start) if args.start else None,
        end_date=date.fromisoformat(args.end) if args.end else None,
        initial_cash=args.initial_cash,
        fees=args.fees,
        slippage=args.slippage,
        output_dir=Path(args.output_dir),
    )
    result = run_stockie_vectorbt_backtest(request)
    print(f"engine={'vectorbt' if result.used_vectorbt else 'pandas_fallback'}")
    print(f"executed_trades={len(result.trade_plans)}")
    print(f"closed_trades={len(result.trades)}")
    print(f"summary={result.output_paths['summary']}")
    print(result.metrics)


if __name__ == "__main__":
    main()

