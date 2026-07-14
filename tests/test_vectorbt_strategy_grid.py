from __future__ import annotations

import unittest

import pandas as pd

from backtest.vectorbt_research.strategy_grid import (
    CALL,
    PUT,
    FLAT,
    leaderboard_row,
    ma_spread_variant,
    research_prediction_rows,
    rsi_reversion_variant,
)


class VectorBTStrategyGridTests(unittest.TestCase):
    def test_ma_spread_variant_emits_call_and_put(self) -> None:
        df = pd.DataFrame({
            "ma10": [101.0, 99.0, 100.0],
            "ma20": [100.0, 100.0, 100.0],
            "rsi14": [55.0, 45.0, 50.0],
        })
        variant = ma_spread_variant("test", 0.005, 60, 40)

        signal = variant.signal_fn(df)

        self.assertEqual(signal.iloc[0], CALL)
        self.assertEqual(signal.iloc[1], PUT)

    def test_rsi_reversion_variant_emits_edges(self) -> None:
        df = pd.DataFrame({"rsi14": [39.0, 50.0, 61.0]})
        variant = rsi_reversion_variant("rsi", 40, 60)

        signal = variant.signal_fn(df)

        self.assertEqual(signal.iloc[0], CALL)
        self.assertEqual(signal.iloc[2], PUT)

    def test_leaderboard_row_reports_total_fires(self) -> None:
        eligible = pd.DataFrame({
            "next_open": [100.0, 100.0, 100.0, 100.0],
            "next_high": [101.0, 100.2, 100.1, 100.1],
            "next_low": [99.5, 99.0, 99.9, 99.9],
            "regime": ["stress", "stress", "stress", "stress"],
            "raw_signal_quality": [1.0, 1.0, 1.0, 1.0],
            "actual_trade_label": [CALL, PUT, CALL, FLAT],
        })
        signal = pd.Series([CALL, PUT, CALL, FLAT])

        row = leaderboard_row(
            "BollingerMeanReversion",
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
        variant = rsi_reversion_variant("RsiMeanReversion_6040", 40, 60)
        eligible = pd.DataFrame({
            "signal_date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "next_trade_date": ["2026-01-02", "2026-01-03", "2026-01-04"],
            "regime": ["stress", "calm", "stress"],
            "rsi14": [35.0, 50.0, 65.0],
            "global_us_return_mean": [0.01, 0.02, -0.01],
            "global_europe_return_mean": [0.001, 0.002, -0.001],
            "global_asia_return_mean": [0.003, 0.004, -0.003],
            "actual_trade_label": [CALL, FLAT, PUT],
            "actual_quality_label": [CALL, "NO_POSITION", PUT],
        })
        signal = pd.Series([CALL, FLAT, PUT])

        rows = research_prediction_rows(variant, eligible, signal)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows.columns[:12].tolist(), [
            "signal_date", "trade_date", "strategy_variant", "strategy_family",
            "strategy_type", "regime", "predicted", "actual_label",
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
