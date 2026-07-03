"""Export in-sample prediction misses with signal-day and execution-day features."""

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
from psycopg2.extras import RealDictCursor

from src.common.config import get_nifty_target_pct, get_settings
from src.technical_analysis.cascade.dataset import build_base


DEFAULT_INPUT = Path("output/backtest/NIFTY/production/NIFTY_prediction.csv")
DEFAULT_OUTPUT = Path(
    "output/backtest/NIFTY/production/NIFTY_stress_in_sample_precision_misses.csv"
)


def _feature_rows(dates: list[date], symbol: str) -> pd.DataFrame:
    import psycopg2

    settings = get_settings()
    if not settings.supabase_conn_str:
        raise RuntimeError("SUPABASE_CONN_STR is required")
    sql = """
        SELECT *
        FROM "SignalFeatureDaily"
        WHERE UPPER(symbol) = %s
          AND feature_version = 'v1'
          AND signal_date = ANY(%s)
        ORDER BY signal_date
    """
    with psycopg2.connect(settings.supabase_conn_str) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol.upper(), dates))
            return pd.DataFrame([dict(row) for row in cur.fetchall()])


def _prefix_features(features: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if features.empty:
        return pd.DataFrame(columns=[f"{prefix}lookup_date"])
    drop = [c for c in ("feature_id", "symbol", "feature_version") if c in features]
    out = features.drop(columns=drop).copy()
    out = out.rename(columns={c: f"{prefix}{c}" for c in out.columns})
    return out.rename(columns={f"{prefix}signal_date": f"{prefix}lookup_date"})


def _why_predicted(row: pd.Series) -> str:
    strategy = str(row.get("primary_strategy") or "")
    if "BollingerMeanReversion" in strategy:
        return (
            f"Bollinger fade fired; RSI14={row.get('signal_feature__rsi14'):.2f}, "
            f"RSI5={row.get('signal_feature__rsi5'):.2f}, "
            f"BB width={100 * row.get('signal_feature__bb_width'):.2f}%."
        )
    if "MomentumDirectional" in strategy:
        return (
            f"Momentum expansion fired; VIX={row.get('vix_close'):.2f}, "
            f"BB width={100 * row.get('signal_feature__bb_width'):.2f}%, "
            f"ret_5d={100 * row.get('signal_feature__ret_5d'):.2f}%, "
            f"ret_10d={100 * row.get('signal_feature__ret_10d'):.2f}%."
        )
    return f"{strategy} was the highest-precision eligible strategy firing that side."


def _miss_reason(row: pd.Series, threshold: float) -> tuple[str, str]:
    up = float(row["up_excursion_pct"])
    down = float(row["down_excursion_pct"])
    gap = (float(row["next_open"]) / float(row["close_1515"]) - 1.0) * 100
    target = threshold * 100
    actual = row["actual_trade_label"]
    prediction = row["final_prediction"]
    if actual == "NO_POSITION":
        category = "TARGET_NOT_REACHED"
        detail = (
            f"Neither side reached the {target:.2f}% stress target over the configured "
            f"horizon (up {up:.2f}%, down {down:.2f}%)."
        )
    elif prediction == "CALL" and gap < 0:
        category = "OVERNIGHT_RISK_REVERSAL"
        detail = (
            f"The CALL faced a {gap:.2f}% opening gap and reversed: "
            f"up excursion {up:.2f}% versus down excursion {down:.2f}%."
        )
    elif max(up, down) >= target and min(up, down) >= target * 0.80:
        category = "WHIPSAW_NEAR_THRESHOLD"
        detail = (
            f"Both directions expanded; the predicted side approached the threshold "
            f"but the opposite side won (up {up:.2f}%, down {down:.2f}%)."
        )
    elif prediction == "CALL" and gap > 0:
        category = "BULLISH_GAP_FAILED"
        detail = (
            f"A {gap:.2f}% bullish opening gap failed; up excursion was {up:.2f}% "
            f"and downside reached {down:.2f}%."
        )
    else:
        category = "WRONG_WAY_REVERSAL"
        detail = f"The opposite side reached target (up {up:.2f}%, down {down:.2f}%)."
    return category, detail


def generate(input_path: Path, output_path: Path, symbol: str, regime: str) -> pd.DataFrame:
    predictions = pd.read_csv(input_path)
    predictions = predictions[predictions["next_open"].notna()].copy()
    fired = predictions[
        predictions["regime"].eq(regime)
        & predictions["final_prediction"].isin(["CALL", "PUT"])
    ].copy()
    correct = (
        fired["final_prediction"].eq("CALL")
        & fired["actual_trade_label"].isin(["CALL", "BOTH"])
    ) | (
        fired["final_prediction"].eq("PUT")
        & fired["actual_trade_label"].isin(["PUT", "BOTH"])
    )
    misses = fired.loc[~correct].copy()

    base = build_base()
    outcomes = base[["signal_date", "future_high_nd", "future_low_nd"]].copy()
    outcomes["signal_date"] = outcomes["signal_date"].astype(str)
    misses = misses.merge(outcomes, on="signal_date", how="left")
    misses["up_excursion_pct"] = (
        (misses["future_high_nd"] - misses["next_open"]) / misses["next_open"] * 100
    )
    misses["down_excursion_pct"] = (
        (misses["next_open"] - misses["future_low_nd"]) / misses["next_open"] * 100
    )

    signal_dates = pd.to_datetime(misses["signal_date"]).dt.date
    trade_dates = pd.to_datetime(misses["next_trade_date"]).dt.date
    features = _feature_rows(sorted(set(signal_dates) | set(trade_dates)), symbol)
    signal_features = _prefix_features(features, "signal_feature__")
    next_features = _prefix_features(features, "next_trade_feature__")
    misses["_signal_lookup"] = signal_dates
    misses["_next_lookup"] = trade_dates
    misses = misses.merge(
        signal_features, left_on="_signal_lookup", right_on="signal_feature__lookup_date", how="left"
    ).merge(
        next_features, left_on="_next_lookup", right_on="next_trade_feature__lookup_date", how="left"
    )

    threshold = get_nifty_target_pct(regime)
    reasons = misses.apply(lambda row: _miss_reason(row, threshold), axis=1)
    misses["why_predicted"] = misses.apply(_why_predicted, axis=1)
    misses["why_missed_category"] = [reason[0] for reason in reasons]
    misses["why_missed"] = [reason[1] for reason in reasons]
    misses = misses.drop(columns=["_signal_lookup", "_next_lookup"])

    front = [
        "signal_date", "next_trade_date", "regime", "final_prediction",
        "actual_trade_label", "primary_strategy", "strategy_precision",
        "strength_score", "up_excursion_pct", "down_excursion_pct",
        "why_predicted", "why_missed_category", "why_missed",
    ]
    misses = misses[front + [c for c in misses.columns if c not in front]]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    misses.to_csv(output_path, index=False)
    return misses


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--regime", default="stress", choices=["stress", "calm"])
    args = parser.parse_args()
    result = generate(args.input, args.output, args.symbol, args.regime)
    print(f"Wrote {len(result)} {args.regime} in-sample precision misses to {args.output}")


if __name__ == "__main__":
    main()
