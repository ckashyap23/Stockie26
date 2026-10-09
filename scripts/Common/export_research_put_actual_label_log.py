"""Export PUT-side research strategy diagnostic view.

Creates a single CSV with one row per signal_date where actual_trade_label is
PUT (and BOTH by default), including the full feature set plus binary
fired/not-fired columns for each active PUT research strategy variant.
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
from src.technical_analysis.cascade.constants import PUT
from src.technical_analysis.cascade.dataset import build_base
from src.technical_analysis.strategy_registry import get_strategy_registry


DEFAULT_OUTPUT = Path("output/backtest/NIFTY/vectorbt_research/research_put_actual_label_log.csv")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def build_put_actual_label_log(
    start: date | None = None,
    end: date | None = None,
    include_both: bool = True,
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
        c for c in base.columns
        if c.endswith("_fired") or c in {"put_research_any_fired", "put_research_fired_count"}
    ]
    if drop_marker_cols:
        base = base.drop(columns=drop_marker_cols, errors="ignore")

    labels = [PUT, "BOTH"] if include_both else [PUT]
    view = base[base["actual_trade_label"].isin(labels)].copy()
    if view.empty:
        return view

    registry = get_strategy_registry()
    put_variants = [
        variant
        for variant in DEFAULT_VARIANTS
        if registry.get_meta(variant.name).direction == PUT
    ]

    for variant in put_variants:
        signal = variant.signal_fn(base).reindex(base.index)
        fired_col = f"{variant.name}_fired"
        fired_on_put_rows = signal.reindex(view.index).eq(PUT)
        view[fired_col] = fired_on_put_rows.astype(int)

    view = view.reset_index(drop=True)
    cols = ["signal_date"] + [c for c in view.columns if c != "signal_date"]
    return view[cols]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export rows where actual_trade_label is PUT (and BOTH by default) "
            "with full features plus PUT research strategy fired flags."
        )
    )
    parser.add_argument("--start", default=None, help="Start signal date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End signal date YYYY-MM-DD")
    parser.add_argument(
        "--include-both",
        default="1",
        choices=["0", "1"],
        help="Include BOTH labels as PUT-side opportunities (default: 1)",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output CSV path")
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    include_both = args.include_both == "1"
    out_path = Path(args.output)

    df = build_put_actual_label_log(start=start, end=end, include_both=include_both)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(
        {
            "output": str(out_path),
            "rows": int(len(df)),
            "include_both": include_both,
            "put_strategy_columns": [
                f"{variant.name}_fired"
                for variant in DEFAULT_VARIANTS
                if get_strategy_registry().get_meta(variant.name).direction == PUT
            ],
        }
    )


if __name__ == "__main__":
    main()
