from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
load_dotenv(_repo_root / ".env")

from src.news_sentiment.config import COMPOSITE_SIGNAL_STORE, SENTIMENT_BACKTEST_DIR

JOINED_OUTPUT = SENTIMENT_BACKTEST_DIR / "sentiment_joined.csv"
SUMMARY_OUTPUT = SENTIMENT_BACKTEST_DIR / "sentiment_residual_summary.txt"


def _load_predictions_from_db(underlying: str = "NIFTY", model_version: str = "cascade_v1") -> pd.DataFrame:
    from src.common.config import get_settings
    from src.data_manager.db.client_factory import get_database_client
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                """
                SELECT next_trade_date, final_prediction, next_return_pct,
                       regime, actual_trade_label
                FROM "NiftyPrediction"
                WHERE symbol = %s AND model_version = %s
                  AND next_trade_date IS NOT NULL
                ORDER BY signal_date
                """,
                (underlying.upper(), model_version),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        db.close()
    return pd.DataFrame(rows, columns=cols)


def run_sentiment_residual_backtest(
    sentiment_csv: Path = COMPOSITE_SIGNAL_STORE,
    joined_output: Path = JOINED_OUTPUT,
    summary_output: Path = SUMMARY_OUTPUT,
    underlying: str = "NIFTY",
    model_version: str = "cascade_v1",
) -> dict[str, object]:
    if not sentiment_csv.exists():
        raise FileNotFoundError(f"Sentiment CSV not found: {sentiment_csv}")

    pred = _load_predictions_from_db(underlying, model_version)
    if pred.empty:
        raise RuntimeError(f"No NiftyPrediction rows found for {underlying} / {model_version}.")
    print(f"Loaded {len(pred)} prediction rows from NiftyPrediction")
    sent = pd.read_csv(sentiment_csv)
    pred["next_trade_date"] = pd.to_datetime(pred["next_trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    sent["target_date"] = pd.to_datetime(sent["target_date"], errors="coerce").dt.strftime("%Y-%m-%d")

    joined = pred.merge(sent, left_on="next_trade_date", right_on="target_date", how="inner")
    joined = joined[pd.to_numeric(joined["next_return_pct"], errors="coerce").notna()].copy()
    if joined.empty:
        raise RuntimeError("No resolved prediction rows overlap sentiment target_date rows.")

    joined["next_return_pct"] = pd.to_numeric(joined["next_return_pct"], errors="coerce")
    joined["composite_score"] = pd.to_numeric(joined["composite_score"], errors="coerce").fillna(0.0)
    joined["ta_direction"] = joined["final_prediction"].map({"CALL": 1, "PUT": -1}).fillna(0).astype(int)
    joined["sentiment_direction"] = joined["composite_label"].map({"positive": 1, "negative": -1}).fillna(0).astype(int)
    joined["sentiment_ta_alignment"] = joined.apply(_alignment, axis=1)

    bucket_mean = joined.groupby(["regime", "final_prediction"], dropna=False)["next_return_pct"].transform("mean")
    joined["ta_expected_return_pct"] = bucket_mean
    joined["residual_return_pct"] = joined["next_return_pct"] - joined["ta_expected_return_pct"]

    joined_output.parent.mkdir(parents=True, exist_ok=True)
    joined.to_csv(joined_output, index=False)
    summary_text = build_summary(joined)
    summary_output.write_text(summary_text, encoding="utf-8")
    print(summary_text)
    print(f"Joined output written to {joined_output}")
    print(f"Summary written to {summary_output}")
    return {"rows": len(joined), "joined_output": str(joined_output), "summary_output": str(summary_output)}


def build_summary(joined: pd.DataFrame) -> str:
    lines = [
        "=" * 72,
        "NIFTY news sentiment residual experiment",
        "=" * 72,
        f"rows: {len(joined)}",
        f"date range: {joined['next_trade_date'].min()} .. {joined['next_trade_date'].max()}",
        "",
        "Overall by sentiment label",
        "-" * 72,
    ]
    lines += _summary_table(joined, "composite_label")
    lines += ["", "TA-silent days only (final_prediction = NO_POSITION)", "-" * 72]
    silent = joined[joined["final_prediction"] == "NO_POSITION"]
    lines += _summary_table(silent, "composite_label") if not silent.empty else ["  no rows"]
    lines += ["", "Sentiment vs technical alignment", "-" * 72]
    lines += _summary_table(joined, "sentiment_ta_alignment")
    lines += ["", "Interpretation notes", "-" * 72]
    lines.append("  residual_return_pct = next_return_pct - mean(next_return_pct | regime, final_prediction)")
    lines.append("  This first pass is descriptive and in-sample; use it to decide whether to build a stricter walk-forward test.")
    return "\n".join(lines) + "\n"


def _summary_table(df: pd.DataFrame, group_col: str) -> list[str]:
    if df.empty:
        return ["  no rows"]
    grouped = df.groupby(group_col, dropna=False)
    rows = []
    header = "  bucket                         n   avg_ret   avg_resid  up_rate  call_label  put_label"
    rows.append(header)
    for bucket, sub in grouped:
        actual = sub["actual_trade_label"].fillna("NO_POSITION")
        rows.append(
            f"  {str(bucket):<28} {len(sub):>4} "
            f"{sub['next_return_pct'].mean():>9.4%} "
            f"{sub['residual_return_pct'].mean():>10.4%} "
            f"{(sub['next_return_pct'] > 0).mean():>8.1%} "
            f"{(actual == 'CALL').mean():>10.1%} "
            f"{(actual == 'PUT').mean():>9.1%}"
        )
    return rows


def _alignment(row: pd.Series) -> str:
    ta = int(row.get("ta_direction") or 0)
    sent = int(row.get("sentiment_direction") or 0)
    if ta == 0 and sent == 0:
        return "both_neutral"
    if ta == 0:
        return "sentiment_only"
    if sent == 0:
        return "technical_only"
    if ta == sent:
        return "agree"
    return "disagree"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest NIFTY news sentiment against technical residual returns.")
    parser.add_argument("--sentiment-csv", default=str(COMPOSITE_SIGNAL_STORE))
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--model-version", default="cascade_v1")
    args = parser.parse_args()
    result = run_sentiment_residual_backtest(
        sentiment_csv=Path(args.sentiment_csv),
        underlying=args.underlying,
        model_version=args.model_version,
    )
    print(result)


if __name__ == "__main__":
    main()
