from __future__ import annotations

import unittest

import pandas as pd

from backtest.vectorbt_research.strategy_grid import (
    CALL,
    PUT,
    FLAT,
    StrategyVariant,
    leaderboard_row,
    research_prediction_rows,
)
from src.technical_analysis.cascade.strategies import (
    bollinger_mean_reversion,
    macd_ema5_20,
)


class VectorBTStrategyGridTests(unittest.TestCase):
    def test_bollinger_mean_reversion_signal(self) -> None:
        df = pd.DataFrame({
            "close_1515": [95.0, 100.0, 106.0],
            "bb_lower": [96.0, 96.0, 96.0],
            "bb_upper": [104.0, 104.0, 104.0],
        })

        signal = bollinger_mean_reversion(df)["strategy_BollingerMeanReversion_signal"]

        self.assertEqual(signal.tolist(), [CALL, FLAT, PUT])

    def test_macd_ema5_20_signal(self) -> None:
        df = pd.DataFrame({
            "close_1515": [100.0] * 8 + [110.0] * 6 + [90.0] * 8,
        })

        signal = macd_ema5_20(df)["strategy_MACD_EMA5_20_signal"]

        self.assertIn(CALL, signal.tolist())
        self.assertIn(PUT, signal.tolist())
        self.assertEqual(signal.iloc[0], FLAT)

    def test_leaderboard_row_reports_total_fires(self) -> None:
        eligible = pd.DataFrame({
            "next_open": [100.0, 100.0, 100.0, 100.0],
            "next_high": [101.0, 100.2, 100.1, 100.1],
            "next_low": [99.5, 99.0, 99.9, 99.9],
            "raw_signal_quality": [1.0, 1.0, 1.0, 1.0],
            "actual_trade_label": [CALL, PUT, CALL, FLAT],
        })
        signal = pd.Series([CALL, PUT, CALL, FLAT])

        row = leaderboard_row(
            "ExpansionVotes_Strong",
            {},
            pd.DataFrame(),
            pd.DataFrame(),
            0.03,
            None,
            eligible=eligible,
            eligible_signal=signal,
        )

        self.assertEqual(row["call_fires"], 2)
        self.assertEqual(row["put_fires"], 1)
        self.assertEqual(row["fires"], 3)

    def test_research_prediction_rows_are_per_fired_variant_date(self) -> None:
        variant = StrategyVariant(
            "ExpansionVotes_Strong",
            lambda df: pd.Series([CALL, FLAT, PUT], index=df.index),
            "test variant",
        )
        eligible = pd.DataFrame({
            "signal_date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "next_trade_date": ["2026-01-02", "2026-01-03", "2026-01-04"],
            "rsi14": [35.0, 50.0, 65.0],
            "global_us_return_mean": [0.01, 0.02, -0.01],
            "global_europe_return_mean": [0.001, 0.002, -0.001],
            "global_asia_return_mean": [0.003, 0.004, -0.003],
            "actual_trade_label": [CALL, FLAT, PUT],
            "actual_quality_label": [CALL, "NO_POSITION", PUT],
        })
        signal = variant.signal_fn(eligible)

        rows = research_prediction_rows(variant, eligible, signal)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows.columns[:12].tolist(), [
            "signal_date", "trade_date", "strategy_variant", "strategy_family",
            "strategy_type", "predicted", "actual_label",
            "quality_label", "us_ret", "europe_ret", "asia_ret",
        ])
        self.assertEqual(rows["signal_date"].tolist(), ["2026-01-01", "2026-01-03"])
        self.assertEqual(rows["trade_date"].tolist(), ["2026-01-02", "2026-01-04"])
        self.assertEqual(rows["predicted"].tolist(), [CALL, PUT])
        self.assertEqual(rows["actual_label"].tolist(), [CALL, PUT])
        self.assertEqual(rows["quality_label"].tolist(), [CALL, PUT])
        self.assertEqual(rows["us_ret"].tolist(), [0.01, -0.01])
        self.assertNotIn("precision", rows.columns)
        self.assertNotIn("qualityBased_precision", rows.columns)


if __name__ == "__main__":
    unittest.main()

