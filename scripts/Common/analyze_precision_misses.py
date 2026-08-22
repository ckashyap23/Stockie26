"""Export precision and recall misses with signal-day feature context."""
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

from src.common.config import get_settings
from src.technical_analysis.cascade.dataset import build_base


DEFAULT_INPUT = Path("output/backtest/NIFTY/production/NIFTY_prediction.csv")
DEFAULT_PRECISION_OUTPUT = Path(
    "output/backtest/NIFTY/production/NIFTY_in_sample_precision_misses.csv"
)
DEFAULT_RECALL_OUTPUT = Path(
    "output/backtest/NIFTY/production/NIFTY_in_sample_recall_misses.csv"
)
_CONTEXT_OFFSETS = (-2, -1, 0, 1, 2)
_REPORT_FRONT_COLUMNS = [
    "signal_date",
    "next_trade_date",
    "effective_prediction",
    "actual_trade_label",
    "quality_label",
    "global_us_return_mean",
    "global_europe_return_mean",
    "global_asia_return_mean",
    "why_predicted",
    "why_missed_category",
    "why_missed",
]


def _predictions_from_db(input_path: Path, symbol: str) -> pd.DataFrame:
    """Load current predictions from the DB, with the CSV as offline fallback."""
    import psycopg2

    settings = get_settings()
    if not settings.supabase_conn_str:
        return pd.read_csv(input_path)

    sql = """
        SELECT *
        FROM "NiftyPrediction"
        WHERE UPPER(symbol) = %s AND model_version = 'cascade_v1'
        ORDER BY signal_date
    """
    with psycopg2.connect(settings.supabase_conn_str) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol.upper(),))
            predictions = pd.DataFrame([dict(row) for row in cur.fetchall()])

    if predictions.empty:
        return pd.read_csv(input_path)

    predictions["signal_date"] = predictions["signal_date"].astype(str)
    if "effective_prediction" not in predictions.columns:
        predictions["effective_prediction"] = predictions.get("final_prediction", "NO_POSITION")
    else:
        predictions["effective_prediction"] = predictions["effective_prediction"].fillna(
            predictions.get("final_prediction")
        )
    return predictions


def _prepare_predictions(input_path: Path, symbol: str) -> pd.DataFrame:
    predictions = _predictions_from_db(input_path, symbol)
    if predictions.empty:
        return predictions
    predictions = predictions[predictions["next_open"].notna()].copy()
    predictions["signal_date"] = pd.to_datetime(predictions["signal_date"]).dt.date
    predictions["next_trade_date"] = pd.to_datetime(
        predictions["next_trade_date"], errors="coerce"
    ).dt.date
    if "effective_prediction" not in predictions.columns:
        predictions["effective_prediction"] = predictions.get("final_prediction", "NO_POSITION")
    else:
        predictions["effective_prediction"] = predictions["effective_prediction"].fillna(
            predictions.get("final_prediction")
        )
    predictions["_final_pred"] = predictions["effective_prediction"].fillna("NO_POSITION")
    return predictions


def _feature_rows(dates: list[date], symbol: str) -> pd.DataFrame:
    import psycopg2

    settings = get_settings()
    if not settings.supabase_conn_str:
        raise RuntimeError("SUPABASE_CONN_STR is required")
    sql = """
        SELECT sfd.*, mf.india_vix AS vix_close
        FROM "SignalFeatureDaily" sfd
        LEFT JOIN "MacroFactorDaily" mf
          ON mf.factor_date = sfd.signal_date AND mf.india_vix IS NOT NULL
        WHERE UPPER(sfd.symbol) = %s
          AND sfd.feature_version = 'v1'
          AND sfd.signal_date = ANY(%s)
        ORDER BY sfd.signal_date
    """
    with psycopg2.connect(settings.supabase_conn_str) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol.upper(), dates))
            return pd.DataFrame([dict(row) for row in cur.fetchall()])


def _prefix_features(features: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if features.empty:
        return pd.DataFrame(columns=[f"{prefix}lookup_date"])
    out = features.copy()
    out["signal_date"] = pd.to_datetime(out["signal_date"]).dt.date
    out = out.rename(columns={"signal_date": "lookup_date"})
    return out.add_prefix(prefix)


def _empty_report(symbol: str) -> pd.DataFrame:
    _ = symbol
    return pd.DataFrame(columns=_REPORT_FRONT_COLUMNS)


def _context_dates(dates: pd.Series, all_dates: list[date]) -> set[date]:
    positions = {day: idx for idx, day in enumerate(all_dates)}
    wanted: set[date] = set()
    for day in dates.dropna():
        idx = positions.get(day)
        if idx is None:
            continue
        for offset in _CONTEXT_OFFSETS:
            pos = idx + offset
            if 0 <= pos < len(all_dates):
                wanted.add(all_dates[pos])
    return wanted


def _attach_signal_features(rows: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if rows.empty:
        return rows
    dates = pd.to_datetime(rows["signal_date"], errors="coerce").dt.date.dropna().tolist()
    if not dates:
        return rows
    features = _feature_rows(dates, symbol)
    if features.empty:
        return rows
    signal_features = _prefix_features(features, "signal_feature_")
    return rows.merge(
        signal_features,
        left_on="signal_date",
        right_on="signal_feature_lookup_date",
        how="left",
    )


def _report_columns(rows: pd.DataFrame) -> pd.DataFrame:
    for col in _REPORT_FRONT_COLUMNS:
        if col not in rows.columns:
            rows[col] = pd.NA
    front = list(_REPORT_FRONT_COLUMNS)
    feature_cols = [
        col for col in rows.columns
        if col.startswith("signal_feature_")
        and col not in {"signal_feature_symbol", "signal_feature_feature_version"}
    ]
    rows = rows[front + feature_cols].copy()
    for col in ("signal_date", "next_trade_date", "signal_feature_lookup_date"):
        if col in rows.columns:
            rows[col] = rows[col].astype(str)
    return rows


def _build_context_report(
    predictions: pd.DataFrame,
    misses: pd.DataFrame,
    miss_reasons: pd.DataFrame,
    symbol: str,
    keep_only: str,
) -> pd.DataFrame:
    all_dates = sorted(predictions["signal_date"].dropna().unique())
    wanted = _context_dates(misses["signal_date"], all_dates)
    rows = predictions[predictions["signal_date"].isin(wanted)].copy()
    _ = keep_only
    rows = rows.merge(miss_reasons, on="signal_date", how="left")
    rows = _attach_signal_features(rows, symbol)
    return _report_columns(rows)


def _safe_float(row: pd.Series, *names: str) -> float:
    for name in names:
        value = row.get(name)
        if value is not None and not pd.isna(value):
            return float(value)
    return float("nan")


def _fmt_float(value: float, fmt: str = ".2f", fallback: str = "n/a") -> str:
    return fallback if pd.isna(value) else format(value, fmt)


def _why_predicted(row: pd.Series) -> str:
    strategy = str(row.get("primary_strategy") or "")
    if "PullbackCall" in strategy:
        rp10 = _safe_float(row, "signal_feature_range_position_10d", "range_position_10d")
        room = _safe_float(row, "signal_feature_resistance_distance_10d", "resistance_distance_10d")
        return f"Pullback CALL fired; range_position_10d={_fmt_float(rp10, '.3f')}, resistance_room={_fmt_float(room, '.3f')}."
    if "DeclineContinuationPut" in strategy:
        ret3 = _safe_float(row, "signal_feature_ret_3d", "ret_3d")
        ma5 = _safe_float(row, "signal_feature_ma5d_slope", "ma5d_slope")
        return f"Decline continuation PUT fired; ret_3d={_fmt_float(ret3, '.4f')}, ma5d_slope={_fmt_float(ma5, '.4f')}."
    if "BreakdownPut" in strategy:
        bb_width = _safe_float(row, "signal_feature_bb_width", "bb_width")
        return f"Breakdown PUT fired; BB width={_fmt_float(100 * bb_width)}%."
    if "RsiReversion" in strategy:
        rsi14 = _safe_float(row, "signal_feature_rsi14", "rsi14")
        return f"RSI reversion fired; RSI14={_fmt_float(rsi14)}."
    if "DRIFT_PROBE" in strategy:
        drift = _safe_float(row, "signal_feature_nifty_drift_pct", "nifty_drift_pct")
        return f"DRIFT_PROBE fired; nifty_drift_pct={_fmt_float(100 * drift)}%."
    return f"Production strategy fired: {strategy or 'unknown'}."


def _miss_reason(row: pd.Series) -> tuple[str, str]:
    predicted = row.get("_final_pred")
    actual = row.get("actual_trade_label")
    up = row.get("up_excursion_pct")
    down = row.get("down_excursion_pct")
    if predicted == actual:
        return "LABEL_MISMATCH", "Prediction and actual label matched but row was selected as a miss."
    if actual == "NO_POSITION":
        return "TARGET_NOT_REACHED", f"Neither side reached target (up {up:.2f}%, down {down:.2f}%)."
    return "WRONG_WAY_REVERSAL", f"The opposite side reached target (up {up:.2f}%, down {down:.2f}%)."


def _why_not_predicted(row: pd.Series, actual_direction: str) -> str:
    if actual_direction == "CALL":
        rsi = _safe_float(row, "rsi14")
        rp10 = _safe_float(row, "range_position_10d")
        room = _safe_float(row, "resistance_distance_10d")
        drift = _safe_float(row, "nifty_drift_pct")
        return (
            "No production CALL strategy fired. "
            f"RSI14={_fmt_float(rsi)}, range_position_10d={_fmt_float(rp10, '.3f')}, "
            f"resistance_room={_fmt_float(room, '.3f')}, nifty_drift_pct={_fmt_float(100 * drift)}%."
        )
    ma5 = _safe_float(row, "ma5d_slope")
    rp10 = _safe_float(row, "range_position_10d")
    bbw = _safe_float(row, "bb_width")
    drift = _safe_float(row, "nifty_drift_pct")
    return (
        "No production PUT strategy fired. "
        f"ma5d_slope={_fmt_float(ma5, '.4f')}, range_position_10d={_fmt_float(rp10, '.3f')}, "
        f"bb_width={_fmt_float(bbw, '.4f')}, nifty_drift_pct={_fmt_float(100 * drift)}%."
    )


def _why_missed_recall(row: pd.Series, actual_direction: str) -> str:
    return (
        f"Actual {actual_direction} reached the target, but no current production strategy "
        "produced an actionable prediction for this feature profile."
    )


def generate(
    input_path: Path,
    output_path: Path,
    symbol: str,
) -> pd.DataFrame:
    predictions = _prepare_predictions(input_path, symbol)
    if predictions.empty:
        result = _empty_report(symbol)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_path, index=False)
        return result

    fired = predictions[predictions["_final_pred"].isin(["CALL", "PUT"])].copy()
    correct = (
        fired["_final_pred"].eq("CALL") & fired["actual_trade_label"].isin(["CALL", "BOTH"])
    ) | (
        fired["_final_pred"].eq("PUT") & fired["actual_trade_label"].isin(["PUT", "BOTH"])
    )
    misses = fired.loc[~correct].copy()
    if misses.empty:
        result = _empty_report(symbol)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_path, index=False)
        return result

    base = build_base()
    outcomes = base[["signal_date", "future_high_nd", "future_low_nd"]].copy()
    outcomes["signal_date"] = pd.to_datetime(outcomes["signal_date"]).dt.date
    misses = misses.merge(outcomes, on="signal_date", how="left")
    misses["up_excursion_pct"] = (
        (misses["future_high_nd"] - misses["next_open"]) / misses["next_open"] * 100
    )
    misses["down_excursion_pct"] = (
        (misses["next_open"] - misses["future_low_nd"]) / misses["next_open"] * 100
    )

    features = _feature_rows(misses["signal_date"].dropna().tolist(), symbol)
    signal_features = _prefix_features(features, "signal_feature_")
    misses = misses.merge(
        signal_features,
        left_on="signal_date",
        right_on="signal_feature_lookup_date",
        how="left",
    )

    reasons = misses.apply(_miss_reason, axis=1)
    misses["why_predicted"] = misses.apply(_why_predicted, axis=1)
    misses["why_missed_category"] = [reason[0] for reason in reasons]
    misses["why_missed"] = [reason[1] for reason in reasons]

    miss_reasons = misses[[
        "signal_date", "why_predicted", "why_missed_category", "why_missed",
    ]].copy()
    result = _build_context_report(predictions, misses, miss_reasons, symbol, keep_only="fired")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)
    return result


def generate_recall_misses(
    input_path: Path,
    output_path: Path,
    symbol: str,
) -> pd.DataFrame:
    """Rows where actual_trade_label is CALL/PUT but we predicted NO_POSITION."""
    predictions = _prepare_predictions(input_path, symbol)
    if predictions.empty:
        result = _empty_report(symbol)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_path, index=False)
        return result

    recall_misses = predictions[
        predictions["_final_pred"].eq("NO_POSITION")
        & predictions["actual_trade_label"].isin(["CALL", "PUT"])
    ].copy()

    if recall_misses.empty:
        result = _empty_report(symbol)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_path, index=False)
        return result

    features = _feature_rows(recall_misses["signal_date"].dropna().tolist(), symbol)
    features_flat = features.copy()
    if not features_flat.empty:
        if "feature_id" in features_flat.columns:
            features_flat = features_flat.drop(columns=["feature_id"])
        features_flat["signal_date"] = pd.to_datetime(features_flat["signal_date"]).dt.date
        recall_misses = recall_misses.merge(
            features_flat,
            on="signal_date",
            how="left",
            suffixes=("", "_feat"),
        )

    recall_misses["why_not_predicted"] = recall_misses.apply(
        lambda row: _why_not_predicted(row, row["actual_trade_label"]), axis=1
    )
    recall_misses["why_missed"] = recall_misses.apply(
        lambda row: _why_missed_recall(row, row["actual_trade_label"]), axis=1
    )
    recall_misses["why_predicted"] = (
        "No actionable CALL/PUT prediction. " + recall_misses["why_not_predicted"]
    )
    recall_misses["why_missed_category"] = "RECALL_MISS"

    miss_reasons = recall_misses[[
        "signal_date", "why_predicted", "why_missed_category", "why_missed",
    ]].copy()
    result = _build_context_report(predictions, recall_misses, miss_reasons, symbol, keep_only="nofired")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate precision and recall miss CSVs for in-sample analysis."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--precision-output", type=Path, default=DEFAULT_PRECISION_OUTPUT)
    parser.add_argument("--recall-output", type=Path, default=DEFAULT_RECALL_OUTPUT)
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--skip-precision", action="store_true")
    parser.add_argument("--skip-recall", action="store_true")
    args = parser.parse_args()

    if not args.skip_precision:
        result = generate(args.input, args.precision_output, args.symbol)
        print(f"Wrote {len(result)} precision misses -> {args.precision_output}")

    if not args.skip_recall:
        result = generate_recall_misses(args.input, args.recall_output, args.symbol)
        print(f"Wrote {len(result)} recall misses -> {args.recall_output}")


if __name__ == "__main__":
    main()
