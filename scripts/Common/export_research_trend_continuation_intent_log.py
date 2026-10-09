"""Export full signal-date diagnostics for TrendPullbackCall setup vs fires.

Creates one CSV with all signal dates in range, the full feature row, actual_trade_label,
a trend setup flag, and fired flag for TrendPullbackCall.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from dotenv import load_dotenv

load_dotenv(_repo_root / ".env")

import pandas as pd

from backtest.vectorbt_research.strategy_grid import (
    DEFAULT_VARIANTS,
    _call_intent_mask,
    load_call_intent_thresholds,
)
from src.technical_analysis.cascade.constants import CALL
from src.technical_analysis.cascade.dataset import build_base


DEFAULT_OUTPUT = Path(
    "output/backtest/NIFTY/vectorbt_research/research_trend_continuation_intent_log.csv"
)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _variant(name: str):
    for variant in DEFAULT_VARIANTS:
        if variant.name == name:
            return variant
    raise RuntimeError(f"Strategy variant not found in DEFAULT_VARIANTS: {name}")


def build_trend_continuation_intent_log(
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    base = build_base().copy()
    base["signal_date"] = pd.to_datetime(base["signal_date"], errors="coerce").dt.date

    if start is not None:
        base = base[base["signal_date"] >= start]
    if end is not None:
        base = base[base["signal_date"] <= end]

    if "actual_trade_label" not in base.columns:
        base["actual_trade_label"] = pd.NA

    thresholds = load_call_intent_thresholds()
    intent = _call_intent_mask("TrendPullbackCall", base, thresholds)

    variant = _variant("TrendPullbackCall")
    fired = variant.signal_fn(base).reindex(base.index).eq(CALL)

    out = base.copy()
    # Provide stable global market aliases for downstream diagnostics.
    if "us_ret" not in out.columns and "global_us_return_mean" in out.columns:
        out["us_ret"] = out["global_us_return_mean"]
    if "europe_ret" not in out.columns and "global_europe_return_mean" in out.columns:
        out["europe_ret"] = out["global_europe_return_mean"]
    if "asia_overnight_ret" not in out.columns and "global_asia_overnight_return_mean" in out.columns:
        out["asia_overnight_ret"] = out["global_asia_overnight_return_mean"]
    if "asia_partial_ret" not in out.columns and "global_asia_partial_return_mean" in out.columns:
        out["asia_partial_ret"] = out["global_asia_partial_return_mean"]
    if "asia_ret" not in out.columns:
        if "asia_overnight_ret" in out.columns:
            out["asia_ret"] = out["asia_overnight_ret"]
        elif "global_asia_overnight_return_mean" in out.columns:
            out["asia_ret"] = out["global_asia_overnight_return_mean"]

    out["actual_trade_label_raw"] = out["actual_trade_label"]
    out["actual_trade_label"] = out["actual_trade_label_raw"].isin(["CALL", "BOTH"]).astype(int)
    out["TrendPullbackCall_setup_flag"] = intent.astype(int)
    out["TrendPullbackCall_fired"] = fired.astype(int)

    front = [
        "signal_date",
        "actual_trade_label",
        "actual_trade_label_raw",
        "us_ret",
        "europe_ret",
        "asia_ret",
        "asia_partial_ret",
        "asia_overnight_ret",
        "TrendPullbackCall_setup_flag",
        "TrendPullbackCall_fired",
    ]
    remaining = [c for c in out.columns if c not in front]
    out = out[front + remaining].reset_index(drop=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export all signal dates with full features + TrendPullbackCall intent "
            "opportunity and fired flags."
        )
    )
    parser.add_argument("--start", default=None, help="Start signal date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End signal date YYYY-MM-DD")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output CSV path")
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    out_path = Path(args.output)

    df = build_trend_continuation_intent_log(start=start, end=end)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(
        {
            "output": str(out_path),
            "rows": int(len(df)),
            "setup_days": int(df["TrendPullbackCall_setup_flag"].sum()),
            "fires": int(df["TrendPullbackCall_fired"].sum()),
        }
    )


if __name__ == "__main__":
    main()
