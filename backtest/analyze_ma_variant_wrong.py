"""Analyze wrong MAAlignmentRoom_variant predictions in experiment base.csv."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

SIGNAL_COL = "strategy_MAAlignmentRoom_variant_signal"
INPUT = Path("output/backtest/NIFTY/experiment/base.csv")
OUTPUT_CSV = Path("output/backtest/NIFTY/experiment/MAAlignmentRoom_variant_wrong.csv")
OUTPUT_TXT = Path("output/backtest/NIFTY/experiment/MAAlignmentRoom_variant_wrong_patterns.txt")

FEATURE_COLS = [
    "rsi14",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "range_position_5d",
    "range_position_10d",
    "support_distance_10d",
    "resistance_distance_10d",
    "volatility_10d",
    "volatility_20d",
    "trend_efficiency_5d",
    "trend_efficiency_10d",
    "ma5d_slope",
    "ma10d_slope",
    "ma20_slope",
    "selected_regime",
    "hindsight_regime",
    "expected_regime_lag2",
    "next_return_pct",
]


def _regime_counts(series: pd.Series) -> str:
    counts = series.fillna("NA").value_counts()
    return ", ".join(f"{k}={v}" for k, v in counts.items())


def _median_block(df: pd.DataFrame, cols: list[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for col in cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        out[col] = float(s.median()) if s.notna().any() else None
    return out


def main() -> None:
    df = pd.read_csv(INPUT)
    fired = df[df[SIGNAL_COL].isin(["CALL", "PUT"])].copy()
    wrong = fired[fired[SIGNAL_COL] != fired["actual_trade_label"]].copy()
    right = fired[fired[SIGNAL_COL] == fired["actual_trade_label"]].copy()

    lines: list[str] = []
    lines.append("MAAlignmentRoom_variant wrong-prediction analysis")
    lines.append("")
    lines.append(f"Source: {INPUT}")
    lines.append(f"Total experiment rows: {len(df)}")
    lines.append(f"Variant signals: {len(fired)}")
    lines.append(f"  CALL: {int((fired[SIGNAL_COL] == 'CALL').sum())}")
    lines.append(f"  PUT: {int((fired[SIGNAL_COL] == 'PUT').sum())}")
    lines.append(f"Wrong: {len(wrong)} ({100 * len(wrong) / len(fired):.1f}% of signals)")
    lines.append(f"Correct: {len(right)}")
    lines.append("")

    for sig in ("CALL", "PUT"):
        sub = wrong[wrong[SIGNAL_COL] == sig]
        opp = "PUT" if sig == "CALL" else "CALL"
        lines.append(
            f"Wrong {sig}: {len(sub)} "
            f"(actual {opp}: {int((sub['actual_trade_label'] == opp).sum())}, "
            f"actual NO_POSITION: {int((sub['actual_trade_label'] == 'NO_POSITION').sum())})"
        )
    lines.append("")

    wrong_med = _median_block(wrong, FEATURE_COLS)
    right_med = _median_block(right, FEATURE_COLS)

    lines.append("Median feature comparison (wrong vs correct)")
    lines.append("feature | wrong | correct | delta")
    lines.append("--- | ---: | ---: | ---:")
    for col in FEATURE_COLS:
        if col not in wrong_med or col not in right_med:
            continue
        w, r = wrong_med[col], right_med[col]
        if w is None or r is None:
            continue
        lines.append(f"{col} | {w:.4f} | {r:.4f} | {w - r:+.4f}")
    lines.append("")

    lines.append("Regime mix on wrong signals")
    lines.append(f"- hindsight_regime: {_regime_counts(wrong['hindsight_regime'])}")
    lines.append(f"- selected_regime: {_regime_counts(wrong['selected_regime'])}")
    lines.append(f"- expected_regime_lag2: {_regime_counts(wrong['expected_regime_lag2'])}")
    lines.append("")

    # Pattern buckets on wrong CALLs
    wrong_call = wrong[wrong[SIGNAL_COL] == "CALL"]
    wrong_put = wrong[wrong[SIGNAL_COL] == "PUT"]

    def bucket_lines(title: str, subset: pd.DataFrame, rules: list[tuple[str, pd.Series]]) -> None:
        lines.append(title)
        for name, mask in rules:
            hit = subset[mask]
            if len(hit):
                med_ret = pd.to_numeric(hit["next_return_pct"], errors="coerce").median()
                lines.append(
                    f"- {name}: {len(hit)} rows "
                    f"(hindsight RANGE: {int((hit['hindsight_regime'] == 'RANGE').sum())}, "
                    f"med next_return: {med_ret:.2f}%)"
                )
        lines.append("")

    if len(wrong_call):
        rsi = pd.to_numeric(wrong_call["rsi14"], errors="coerce")
        ret5 = pd.to_numeric(wrong_call["ret_5d"], errors="coerce")
        ret10 = pd.to_numeric(wrong_call["ret_10d"], errors="coerce")
        res_dist = pd.to_numeric(wrong_call["resistance_distance_10d"], errors="coerce")
        rng10 = pd.to_numeric(wrong_call["range_position_10d"], errors="coerce")
        bucket_lines(
            "Wrong CALL pattern buckets",
            wrong_call,
            [
                ("RSI 55-65 (late/overbought stack)", (rsi >= 55) & (rsi < 65)),
                ("Short-term already up (ret_5d > 0)", ret5 > 0),
                ("Pullback bounce but trend still down (ret_10d < 0)", ret10 < 0),
                ("Near resistance (<= 1%)", res_dist <= 0.01),
                ("Upper half of 10d range (>= 0.6)", rng10 >= 0.6),
                ("Hindsight was TREND_DOWN", wrong_call["hindsight_regime"] == "TREND_DOWN"),
                ("Hindsight was RANGE", wrong_call["hindsight_regime"] == "RANGE"),
            ],
        )

    if len(wrong_put):
        rsi = pd.to_numeric(wrong_put["rsi14"], errors="coerce")
        ret5 = pd.to_numeric(wrong_put["ret_5d"], errors="coerce")
        ret10 = pd.to_numeric(wrong_put["ret_10d"], errors="coerce")
        sup_dist = pd.to_numeric(wrong_put["support_distance_10d"], errors="coerce")
        rng10 = pd.to_numeric(wrong_put["range_position_10d"], errors="coerce")
        bucket_lines(
            "Wrong PUT pattern buckets",
            wrong_put,
            [
                ("RSI 35-45 (early/weak bear stack)", (rsi > 35) & (rsi <= 45)),
                ("Short-term already down (ret_5d < 0)", ret5 < 0),
                ("Still positive 10d trend (ret_10d > 0)", ret10 > 0),
                ("Near support (<= 1%)", sup_dist <= 0.01),
                ("Lower half of 10d range (<= 0.4)", rng10 <= 0.4),
                ("Hindsight was TREND_UP", wrong_put["hindsight_regime"] == "TREND_UP"),
                ("Hindsight was RANGE", wrong_put["hindsight_regime"] == "RANGE"),
            ],
        )

    # Top overlapping failure pattern across all wrong
    lines.append("Strongest cross-cutting wrong-signal pattern")
    overlap = wrong[
        (wrong["hindsight_regime"] == "RANGE")
        & (pd.to_numeric(wrong["ret_10d"], errors="coerce").abs() < 0.01)
    ]
    lines.append(
        f"- MA stack fired inside RANGE with flat 10d trend: {len(overlap)} / {len(wrong)} wrong rows"
    )
    near_extreme = wrong[
        (
            (wrong[SIGNAL_COL] == "CALL")
            & (pd.to_numeric(wrong["resistance_distance_10d"], errors="coerce") <= 0.01)
        )
        | (
            (wrong[SIGNAL_COL] == "PUT")
            & (pd.to_numeric(wrong["support_distance_10d"], errors="coerce") <= 0.01)
        )
    ]
    lines.append(
        f"- Signal into nearby 10d support/resistance (<=1%): {len(near_extreme)} / {len(wrong)} wrong rows"
    )
    late_rsi = wrong[
        ((wrong[SIGNAL_COL] == "CALL") & (pd.to_numeric(wrong["rsi14"], errors="coerce") >= 55))
        | ((wrong[SIGNAL_COL] == "PUT") & (pd.to_numeric(wrong["rsi14"], errors="coerce") <= 45))
    ]
    lines.append(
        f"- Late RSI on stack side (CALL RSI>=55 or PUT RSI<=45): {len(late_rsi)} / {len(wrong)} wrong rows"
    )

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    export_cols = [
        "trade_date",
        SIGNAL_COL,
        "actual_trade_label",
        "next_return_pct",
        "rsi14",
        "ret_5d",
        "ret_10d",
        "range_position_10d",
        "support_distance_10d",
        "resistance_distance_10d",
        "selected_regime",
        "hindsight_regime",
        "expected_regime_lag2",
    ]
    wrong[export_cols].sort_values("trade_date").to_csv(OUTPUT_CSV, index=False)
    OUTPUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\nWrote {OUTPUT_CSV}")
    print(f"Wrote {OUTPUT_TXT}")


if __name__ == "__main__":
    main()
