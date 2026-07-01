from __future__ import annotations

import unittest

import pandas as pd

from src.technical_analysis.prediction.features import compute_atr_sma, compute_underlying_features
from src.technical_analysis.prediction.signal_strength import (
    add_raw_direction,
    signal_quality,
    summarize_signal_quality,
)
from src.technical_analysis.cascade.pipeline import _quality_interpretation_line


class SignalStrengthTests(unittest.TestCase):
    def test_atr14_sma_is_simple_mean_of_true_range(self) -> None:
        frame = pd.DataFrame({
            "high_price": [float(i + 11) for i in range(14)],
            "low_price": [float(i + 9) for i in range(14)],
            "close_price": [float(i + 10) for i in range(14)],
        })

        atr = compute_atr_sma(frame, 14)
        features = compute_underlying_features(frame)

        self.assertAlmostEqual(float(atr.iloc[-1]), 2.0)
        self.assertEqual(features["atr14_sma"], 2.0)

    def test_raw_direction_uses_complete_next_three_sessions(self) -> None:
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
        self.assertAlmostEqual(result.loc[0, "raw_direction"], 1.0 / 7.0)
        self.assertTrue(result.loc[1:, "raw_direction"].isna().all())

    def test_quality_aligns_call_and_put_and_aggregates_fires_only(self) -> None:
        outcomes = pd.DataFrame({"raw_direction": [0.5, -0.25, 0.9]})
        signals = pd.Series(["CALL", "PUT", "NO_POSITION"])

        quality = signal_quality(signals, outcomes["raw_direction"])
        summary = summarize_signal_quality(signals, outcomes)

        self.assertEqual(quality.iloc[0], 0.5)
        self.assertEqual(quality.iloc[1], 0.25)
        self.assertTrue(pd.isna(quality.iloc[2]))
        self.assertEqual(summary["quality_scored_fires"], 2)
        self.assertEqual(summary["positive_quality_rate_pct"], 100.0)
        self.assertEqual(summary["mean_signal_quality"], 0.375)

    def test_zero_atr_is_not_scorable(self) -> None:
        frame = pd.DataFrame({
            "close_1515": [100.0] * 4,
            "high_day": [101.0] * 4,
            "low_day": [99.0] * 4,
            "atr14_sma": [0.0] * 4,
        })
        self.assertTrue(add_raw_direction(frame)["raw_direction"].isna().all())

    def test_quality_interpretation_explains_near_neutral_walk_forward_score(self) -> None:
        line = _quality_interpretation_line(0.04, 55.2)

        self.assertIn("slightly positive, close-to-neutral", line)
        self.assertIn("a modest majority", line)


if __name__ == "__main__":
    unittest.main()
