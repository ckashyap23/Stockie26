from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from backtest.vectorbt_trades.runner import build_signal_matrices_from_fills, run_vectorbt_or_fallback
from backtest.vectorbt_trades.data_adapter import apply_current_policy_levels
from backtest.vectorbt_trades.service import _enrich_trades


class StockieVectorBTAdapterTest(unittest.TestCase):
    def test_enriched_replay_uses_actual_prices_and_removes_duplicate_fee_columns(self) -> None:
        engine_trades = pd.DataFrame([{
            "Column": 0,
            "Avg Entry Price": 999.0,
            "Avg Exit Price": 888.0,
            "Entry Fees": 1.0,
            "Exit Fees": 2.0,
        }])
        fills = pd.DataFrame([{
            "trade_id": "trade-1",
            "entry_price": 409.4,
            "exit_price": 418.25,
            "entry_charges": 35.78,
            "exit_charges": 75.81,
        }])

        enriched = _enrich_trades(engine_trades, fills, used_vectorbt=True)

        self.assertEqual(enriched.loc[0, "entry_price"], 409.4)
        self.assertEqual(enriched.loc[0, "exit_price"], 418.25)
        self.assertNotIn("planned_entry_price", enriched.columns)
        self.assertNotIn("Entry Fees", enriched.columns)
        self.assertNotIn("Exit Fees", enriched.columns)
        self.assertNotIn("Avg Entry Price", enriched.columns)
        self.assertNotIn("Avg Exit Price", enriched.columns)

    def test_replay_levels_use_current_env_policy_and_actual_fill(self) -> None:
        fills = pd.DataFrame([{
            "entry_price": 409.4,
            "regime": "calm",
            "exit_price": 418.25,
            "exit_reason": "STOP_LOSS_HIT",
            "quantity": 65,
            "lot_size": 65,
            "pnl_points": 8.85,
            "total_charges": 111.60,
        }])

        with patch.dict("os.environ", {"CALM_TARGET_PCT": "0.05", "CALM_SL_PCT": "0.02"}):
            replay = apply_current_policy_levels(fills)

        self.assertEqual(replay.loc[0, "target_1_pct"], 0.05)
        self.assertEqual(replay.loc[0, "stop_loss_pct"], 0.02)
        self.assertAlmostEqual(replay.loc[0, "target_1_price"], 429.87, places=2)
        self.assertAlmostEqual(replay.loc[0, "stop_loss_price"], 401.21, places=2)
        self.assertEqual(replay.loc[0, "exit_price"], 418.25)
        self.assertEqual(replay.loc[0, "exit_reason"], "STOP_LOSS_HIT")
        self.assertEqual(replay.loc[0, "lot_count"], 3)
        self.assertAlmostEqual(replay.loc[0, "gross_pnl"], 1725.75, places=2)
        self.assertAlmostEqual(replay.loc[0, "net_pnl"], 1614.15, places=2)

    def test_build_signal_matrices_from_actual_fills(self) -> None:
        fills = pd.DataFrame([{
            "trade_id": "2026-06-24_1",
            "entry_time": "2026-06-25 09:15:00",
            "entry_price": 100.0,
            "exit_time": "2026-06-25 10:00:00",
            "exit_price": 106.0,
        }])

        price, entries, exits = build_signal_matrices_from_fills(fills)

        self.assertTrue(entries.loc[pd.Timestamp("2026-06-25 09:15:00"), "2026-06-24_1"])
        self.assertTrue(exits.loc[pd.Timestamp("2026-06-25 10:00:00"), "2026-06-24_1"])
        self.assertEqual(price.loc[pd.Timestamp("2026-06-25 09:15:00"), "2026-06-24_1"], 100.0)
        self.assertEqual(price.loc[pd.Timestamp("2026-06-25 10:00:00"), "2026-06-24_1"], 106.0)

    def test_fallback_replay_returns_trade_metrics(self) -> None:
        idx = pd.to_datetime(["2026-06-25 09:15:00", "2026-06-25 15:15:00"])
        price = pd.DataFrame({"trade": [100.0, 110.0]}, index=idx)
        entries = pd.DataFrame({"trade": [True, False]}, index=idx)
        exits = pd.DataFrame({"trade": [False, True]}, index=idx)

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "vectorbt":
                raise ImportError("vectorbt intentionally disabled for fallback test")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            trades, metrics, used_vectorbt = run_vectorbt_or_fallback(
                price=price,
                entries=entries,
                exits=exits,
                initial_cash=100_000,
                fees=0.0,
                slippage=0.0,
                closed_trades=pd.DataFrame([{
                    "trade_id": "trade",
                    "entry_price": 100.0,
                    "exit_price": 110.0,
                    "lot_size": 1,
                }]),
            )

        self.assertIn("trades", metrics)
        self.assertEqual(metrics["trades"], 1)
        self.assertFalse(used_vectorbt)
        self.assertEqual(float(trades.iloc[0]["pnl_per_unit"]), 10.0)


if __name__ == "__main__":
    unittest.main()

