"""Export production strategy features, directional predictions, fires, and labels."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.technical_analysis.cascade.constants import CALL, PUT
from src.technical_analysis.cascade.dataset import build_base
from src.technical_analysis.cascade.engine import gather_signals
from src.technical_analysis.cascade.global_index_features import (
    build_global_index_features_cumulative,
    load_global_index_rows,
)
from src.technical_analysis.cascade.strategies import ALL_PRODUCTION_STRATEGIES
from src.technical_analysis.strategy_registry import get_strategy_registry


DEFAULT_OUTPUT = (
    Path("output")
    / "backtest"
    / "NIFTY"
    / "production"
    / "production_strategy_metrics_log.csv"
)

FORWARD_LOOKING_COLUMNS = {
    "next_open",
    "next_high",
    "next_low",
    "next_close",
    "next_return_pct",
    "future_high_nd",
    "future_low_nd",
}


def _add_raw_global_columns(base: pd.DataFrame) -> pd.DataFrame:
    signal_dates = pd.to_datetime(base["signal_date"], errors="coerce")
    valid_dates = signal_dates.dropna()
    if valid_dates.empty:
        return base

    global_rows = load_global_index_rows(
        valid_dates.min().date(),
        (valid_dates.max() + pd.Timedelta(days=5)).date(),
    )
    if global_rows.empty:
        return base

    global_features = build_global_index_features_cumulative(
        global_rows,
        pd.DatetimeIndex(sorted(valid_dates.unique())),
    )
    raw_columns = [
        column for column in global_features.columns if column.startswith("global_ret_")
    ]
    if not raw_columns:
        return base

    raw_global = global_features[["trade_date", *raw_columns]].rename(
        columns={"trade_date": "signal_date"}
    )
    out = base.copy()
    out["signal_date"] = pd.to_datetime(out["signal_date"], errors="coerce")
    raw_global["signal_date"] = pd.to_datetime(raw_global["signal_date"], errors="coerce")
    return out.merge(raw_global, on="signal_date", how="left")


def build_production_strategy_metrics_log(
    start: date,
    end: date,
) -> pd.DataFrame:
    if start > end:
        raise ValueError("start must be on or before end")

    base = build_base().copy()
    signals = gather_signals(base, ALL_PRODUCTION_STRATEGIES)
    registry = get_strategy_registry()

    strategy_columns: list[str] = []
    for name in sorted(signals):
        try:
            meta = registry.get_meta(name)
        except KeyError:
            continue
        if meta.strategy_type != "SIGNAL":
            continue
        prediction_column = f"strategy_{name}_prediction"
        fire_column = f"strategy_{name}_fired"
        prediction = signals[name].reindex(base.index)
        base[prediction_column] = prediction
        base[fire_column] = prediction.isin([CALL, PUT]).astype(int)
        strategy_columns.extend([prediction_column, fire_column])

    base = _add_raw_global_columns(base)
    base["signal_date"] = pd.to_datetime(base["signal_date"], errors="coerce").dt.date
    base = base[
        base["signal_date"].between(start, end, inclusive="both")
    ].copy()

    excluded = FORWARD_LOOKING_COLUMNS | set(strategy_columns) | {
        "signal_date",
        "actual_trade_label",
        "actual_trade_label_relaxed",
    }
    feature_columns = [
        column for column in base.columns if column not in excluded
    ]
    global_columns = sorted(
        column for column in feature_columns if column.startswith("global_")
    )
    non_global_features = [
        column for column in feature_columns if column not in global_columns
    ]
    columns = [
        "signal_date",
        *non_global_features,
        *global_columns,
        *strategy_columns,
        "actual_trade_label",
        "actual_trade_label_relaxed",
    ]
    return base.reindex(columns=columns).sort_values("signal_date").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export every production signal date with point-in-time features, "
            "global-index context, binary production-strategy fires, and actual label."
        )
    )
    parser.add_argument("--start", required=True, help="Start signal date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End signal date YYYY-MM-DD")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output CSV path")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    output_path = Path(args.output)
    frame = build_production_strategy_metrics_log(start=start, end=end)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)

    strategy_columns = [
        column for column in frame.columns
        if column.startswith("strategy_")
        and column.endswith(("_prediction", "_fired"))
    ]
    global_columns = [
        column for column in frame.columns if column.startswith("global_")
    ]
    print(
        {
            "output": str(output_path),
            "rows": len(frame),
            "start": str(frame["signal_date"].min()) if not frame.empty else None,
            "end": str(frame["signal_date"].max()) if not frame.empty else None,
            "feature_columns": len(frame.columns) - len(strategy_columns) - 2,
            "global_columns": len(global_columns),
            "production_strategies": len(strategy_columns) // 2,
        }
    )


if __name__ == "__main__":
    main()
