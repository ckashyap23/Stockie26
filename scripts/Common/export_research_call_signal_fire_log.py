"""Export CALL-side fired-date research strategy diagnostic view.

Creates a single CSV with one row per signal_date where any active CALL
research strategy fired, including the full feature set, actual_trade_label,
and binary fired/not-fired columns for each active CALL research strategy.
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

from backtest.vectorbt_research.strategy_grid import DEFAULT_VARIANTS
from src.technical_analysis.cascade.constants import CALL
from src.technical_analysis.cascade.dataset import build_base
from src.technical_analysis.strategy_registry import get_strategy_registry


DEFAULT_OUTPUT = Path("output/backtest/NIFTY/vectorbt_research/research_call_signal_fire_log.csv")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def build_call_signal_fire_log(
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    base = build_base().copy()
    if "trend_efficiency_20d" not in base.columns:
        base["trend_efficiency_20d"] = pd.NA
    base["signal_date"] = pd.to_datetime(base["signal_date"], errors="coerce").dt.date

    if start is not None:
        base = base[base["signal_date"] >= start]
    if end is not None:
        base = base[base["signal_date"] <= end]

    drop_marker_cols = [
        c
        for c in base.columns
        if c.endswith("_fired")
        or c
        in {
            "call_research_any_fired",
            "call_research_fired_count",
            "put_research_any_fired",
            "put_research_fired_count",
        }
    ]
    if drop_marker_cols:
        base = base.drop(columns=drop_marker_cols, errors="ignore")

    registry = get_strategy_registry()
    call_variants = [
        variant
        for variant in DEFAULT_VARIANTS
        if registry.get_meta(variant.name).direction == CALL
    ]

    fire_cols: list[str] = []
    for variant in call_variants:
        signal = variant.signal_fn(base).reindex(base.index)
        fired_col = f"{variant.name}_fired"
        base[fired_col] = signal.eq(CALL).astype(int)
        fire_cols.append(fired_col)

    if not fire_cols:
        return base.iloc[0:0].copy()

    any_call_fire = base[fire_cols].sum(axis=1) > 0
    view = base[any_call_fire].copy()
    if view.empty:
        return view

    view = view.reset_index(drop=True)
    cols = ["signal_date"] + [c for c in view.columns if c != "signal_date"]
    return view[cols]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export rows where any CALL research strategy fired with full "
            "features, actual_trade_label, and CALL strategy fired flags."
        )
    )
    parser.add_argument("--start", default=None, help="Start signal date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End signal date YYYY-MM-DD")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output CSV path")
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    out_path = Path(args.output)

    df = build_call_signal_fire_log(start=start, end=end)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(
        {
            "output": str(out_path),
            "rows": int(len(df)),
            "call_strategy_columns": [
                f"{variant.name}_fired"
                for variant in DEFAULT_VARIANTS
                if get_strategy_registry().get_meta(variant.name).direction == CALL
            ],
        }
    )


if __name__ == "__main__":
    main()
