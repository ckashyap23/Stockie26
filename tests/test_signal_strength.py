from __future__ import annotations

import unittest

import pandas as pd

from src.technical_analysis.prediction.features import compute_atr_sma, compute_underlying_features
from src.technical_analysis.prediction.signal_strength import (
    add_raw_direction,
    quality_label_metrics,
    signal_quality,
    summarize_signal_quality,
)
from src.technical_analysis.cascade.pipeline import _quality_interpretation_line
from scripts.daily_NIFTY.daily_nifty_prediction import _frame_to_rows


class SignalStrengthTests(unittest.TestCase):
    def test_atr7_and_atr14_features_use_true_range(self) -> None:
        frame = pd.DataFrame({
            "high_price": [float(i + 11) for i in range(14)],
            "low_price": [float(i + 9) for i in range(14)],
            "close_price": [float(i + 10) for i in range(14)],
        })

        atr = compute_atr_sma(frame, 14)
        atr7 = compute_atr_sma(frame, 7)
        features = compute_underlying_features(frame)

        self.assertAlmostEqual(float(atr.iloc[-1]), 2.0)
        self.assertAlmostEqual(float(atr7.iloc[-1]), 2.0)
        self.assertEqual(features["atr7"], 2.0)
        self.assertEqual(features["atr7_sma"], 2.0)
        self.assertEqual(features["atr14_sma"], 2.0)

    def test_raw_signal_quality_uses_complete_next_three_sessions(self) -> None:
        frame = pd.DataFrame({
            "close_1515": [100.0, 101.0, 102.0, 103.0],
            "high_day": [101.0, 102.0, 104.0, 103.0],
            "low_day": [99.0, 99.0, 98.0, 97.0],
            "atr14_sma": [2.0, 2.0, 2.0, 2.0],
        })

        result = add_raw_direction(frame)

        self.assertEqual(result.loc[0, "future_high_3d"], 104.0)
        self.assertEqual(result.loc[0, "future_low_3d"], 97.0)
        self.assertAlmostEqual(result.loc[0, "bull_score"], 2.0)
        self.assertAlmostEqual(result.loc[0, "bear_score"], 1.5)
        self.assertAlmostEqual(result.loc[0, "raw_signal_quality"], 1.0 / 7.0)
        self.assertEqual(result.loc[0, "actual_quality_label"], "CALL")
        self.assertTrue(result.loc[1:, "raw_signal_quality"].isna().all())
        self.assertTrue(result.loc[1:, "actual_quality_label"].eq("NO_POSITION").all())

    def test_actual_quality_label_uses_dominant_score_direction(self) -> None:
        frame = pd.DataFrame({
            "close_1515": [100.0, 100.0, 100.0, 100.0],
            "high_day": [100.0, 100.4, 100.3, 100.2],
            "low_day": [100.0, 98.0, 99.0, 99.5],
            "atr14_sma": [2.0, 2.0, 2.0, 2.0],
        })

        result = add_raw_direction(frame, horizon=3)

        self.assertEqual(result.loc[0, "actual_quality_label"], "PUT")

    def test_quality_aligns_call_and_put_and_aggregates_fires_only(self) -> None:
        outcomes = pd.DataFrame({"raw_signal_quality": [0.5, -0.25, 0.9]})
        signals = pd.Series(["CALL", "PUT", "NO_POSITION"])

        quality = signal_quality(signals, outcomes["raw_signal_quality"])
        summary = summarize_signal_quality(signals, outcomes)

        self.assertEqual(quality.iloc[0], 0.5)
        self.assertEqual(quality.iloc[1], 0.25)
        self.assertTrue(pd.isna(quality.iloc[2]))
        self.assertEqual(summary["quality_scored_fires"], 2)
        self.assertEqual(summary["positive_quality_rate_pct"], 100.0)
        self.assertEqual(summary["mean_signal_quality"], 0.375)

    def test_quality_label_precision_recall_and_f1(self) -> None:
        signal = pd.Series(["CALL", "PUT", "CALL", "NO_POSITION"])
        actual = pd.Series(["CALL", "PUT", "PUT", "CALL"])

        metrics = quality_label_metrics(signal, actual)
        call_metrics = quality_label_metrics(signal, actual, side="CALL")

        self.assertAlmostEqual(metrics["qualityBased_precision"], 2 / 3)
        self.assertAlmostEqual(metrics["qualityBased_recall"], 2 / 4)
        self.assertAlmostEqual(metrics["qualityBased_F1"], 4 / 7)
        self.assertAlmostEqual(call_metrics["qualityBased_precision"], 1 / 2)
        self.assertAlmostEqual(call_metrics["qualityBased_recall"], 1 / 2)
        self.assertAlmostEqual(call_metrics["qualityBased_F1"], 1 / 2)

    def test_zero_atr_is_not_scorable(self) -> None:
        frame = pd.DataFrame({
            "close_1515": [100.0] * 4,
            "high_day": [101.0] * 4,
            "low_day": [99.0] * 4,
            "atr14_sma": [0.0] * 4,
        })
        self.assertTrue(add_raw_direction(frame)["raw_signal_quality"].isna().all())

    def test_quality_outcomes_are_preserved_for_prediction_upsert(self) -> None:
        frame = pd.DataFrame({
            "signal_date": ["2026-07-01"],
            "next_trade_date": ["2026-07-02"],
            "bull_score": [1.25],
            "bear_score": [0.75],
            "signal_quality": [0.25],
            "actual_quality_label": ["CALL"],
            "quality_horizon_days": [3],
        })

        row = _frame_to_rows(frame, "NIFTY", "cascade_v1")[0]

        self.assertEqual(row["bull_score"], 1.25)
        self.assertEqual(row["bear_score"], 0.75)
        self.assertEqual(row["signal_quality"], 0.25)
        self.assertEqual(row["actual_quality_label"], "CALL")
        self.assertEqual(row["quality_horizon_days"], 3)

    def test_persisted_signal_quality_formula_is_prediction_independent(self) -> None:
        frame = pd.DataFrame({
            "close_1515": [100.0, 101.0, 102.0, 103.0],
            "high_day": [101.0, 102.0, 104.0, 103.0],
            "low_day": [99.0, 99.0, 98.0, 97.0],
            "atr14_sma": [2.0, 2.0, 2.0, 2.0],
        })

        outcomes = add_raw_direction(frame, horizon=3)

        self.assertAlmostEqual(
            outcomes.loc[0, "raw_signal_quality"],
            (outcomes.loc[0, "bull_score"] - outcomes.loc[0, "bear_score"])
            / (outcomes.loc[0, "bull_score"] + outcomes.loc[0, "bear_score"]),
        )

    def test_quality_interpretation_explains_near_neutral_walk_forward_score(self) -> None:
        line = _quality_interpretation_line(0.04, 55.2)

        self.assertIn("slightly positive, close-to-neutral", line)
        self.assertIn("a modest majority", line)


if __name__ == "__main__":
    unittest.main()
