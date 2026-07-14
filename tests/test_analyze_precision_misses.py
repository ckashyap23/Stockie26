from pathlib import Path

import pandas as pd

from scripts.Common import analyze_precision_misses as misses


def _predictions() -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=5, freq="B")
    rows = []
    for i, signal_date in enumerate(dates):
        rows.append({
            "signal_date": signal_date.date(),
            "next_trade_date": (signal_date + pd.Timedelta(days=1)).date(),
            "next_open": 100.0,
            "next_high": 101.0,
            "next_low": 99.0,
            "regime": "stress",
            "final_prediction": "NO_POSITION",
            "effective_prediction": "NO_POSITION",
            "actual_trade_label": "NO_POSITION",
            "primary_strategy": "",
            "global_us_return_mean": i / 100,
            "global_europe_return_mean": i / 200,
            "global_asia_return_mean": -i / 300,
        })
    return pd.DataFrame(rows)


def _features(dates: list, symbol: str) -> pd.DataFrame:
    rows = []
    for d in dates:
        rows.append({
            "signal_date": d,
            "symbol": symbol,
            "feature_version": "v1",
            "close_1515": 100.0,
            "rsi14": 40.0,
            "bb_width": 0.06,
            "ret_5d": -0.01,
        })
    return pd.DataFrame(rows)


def test_precision_miss_output_expands_context_window_and_limits_columns(tmp_path, monkeypatch):
    predictions = _predictions()
    predictions.loc[2, "final_prediction"] = "CALL"
    predictions.loc[2, "effective_prediction"] = "CALL"
    predictions.loc[2, "actual_trade_label"] = "PUT"
    predictions.loc[2, "primary_strategy"] = "BollingerMeanReversion"

    monkeypatch.setattr(misses, "_predictions_with_promotions", lambda input_path, symbol: predictions)
    monkeypatch.setattr(misses, "_feature_rows", _features)
    monkeypatch.setattr(misses, "get_nifty_target_pct", lambda regime: 0.01)
    monkeypatch.setattr(
        misses,
        "build_base",
        lambda: pd.DataFrame({
            "signal_date": predictions["signal_date"],
            "future_high_nd": [100.2] * len(predictions),
            "future_low_nd": [98.5] * len(predictions),
        }),
    )

    result = misses.generate(Path("unused.csv"), tmp_path / "precision.csv", "NIFTY", "stress")

    assert result["signal_date"].tolist() == [str(d) for d in predictions["signal_date"]]
    assert result.columns.tolist() == [
        "signal_date",
        "next_trade_date",
        "regime",
        "effective_prediction",
        "global_us_return_mean",
        "global_europe_return_mean",
        "global_asia_return_mean",
        "why_predicted",
        "why_missed_category",
        "why_missed",
        "signal_feature_lookup_date",
        "signal_feature_close_1515",
        "signal_feature_rsi14",
        "signal_feature_bb_width",
        "signal_feature_ret_5d",
    ]
    miss_row = result[result["signal_date"].eq(str(predictions.loc[2, "signal_date"]))].iloc[0]
    assert miss_row["why_missed_category"] == "WRONG_WAY_REVERSAL"
    assert "Bollinger fade fired" in miss_row["why_predicted"]
    assert result.loc[0, "why_missed"] != result.loc[0, "why_missed"]


def test_recall_miss_output_uses_same_context_window_columns(tmp_path, monkeypatch):
    predictions = _predictions()
    predictions.loc[2, "actual_trade_label"] = "CALL"

    monkeypatch.setattr(misses, "_predictions_with_promotions", lambda input_path, symbol: predictions)
    monkeypatch.setattr(misses, "_feature_rows", _features)

    result = misses.generate_recall_misses(
        Path("unused.csv"), tmp_path / "recall.csv", "NIFTY", "stress"
    )

    assert result["signal_date"].tolist() == [str(d) for d in predictions["signal_date"]]
    assert "why_not_predicted" not in result.columns
    assert all(column in result.columns for column in misses._REPORT_FRONT_COLUMNS)
    miss_row = result[result["signal_date"].eq(str(predictions.loc[2, "signal_date"]))].iloc[0]
    assert miss_row["why_missed_category"] == "RECALL_MISS"
    assert miss_row["why_predicted"].startswith("No actionable CALL/PUT prediction.")
    assert "signal_feature_rsi14" in result.columns
