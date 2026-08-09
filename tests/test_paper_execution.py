from __future__ import annotations

import unittest
from datetime import datetime, time
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from src.execution.paper import capture_paper_order_charges, resolve_exit_reason
from src.common.config import get_sl_pct_for_regime, get_target_pct_for_regime, normalize_pct
from src.technical_analysis.optionselection.pipeline import target_pcts_for_regime


class PaperExecutionTests(unittest.TestCase):
    def test_paper_order_charges_are_persisted_and_aggregated(self) -> None:
        db = Mock()
        kite = Mock()
        charge_result = {"charges": {"brokerage": 20.0, "total": 24.5}}
        kite.calculate_order_charges.return_value = [charge_result]

        capture_paper_order_charges(
            db, kite, signal_id=7, paper_order_id=11,
            option_symbol="NIFTY26JUL23500CE", side="BUY",
            quantity=65, fill_price=100.0,
        )

        kite.calculate_order_charges.assert_called_once()
        db.update_paper_order_charges.assert_called_once_with(11, charge_result=charge_result)
        db.refresh_paper_trade_costs.assert_called_once_with(7)

    def test_charge_api_failure_does_not_raise(self) -> None:
        db = Mock()
        kite = Mock()
        kite.calculate_order_charges.side_effect = RuntimeError("charges unavailable")

        capture_paper_order_charges(
            db, kite, signal_id=7, paper_order_id=11,
            option_symbol="NIFTY26JUL23500CE", side="SELL",
            quantity=65, fill_price=105.0,
        )

        db.update_paper_order_charges.assert_called_once_with(
            11, error_message="charges unavailable"
        )
        db.refresh_paper_trade_costs.assert_called_once_with(7)

    def test_regime_target_percentages(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "STRESS_TARGET_PCT": "3%",
                "CALM_TARGET_PCT": "0.05",
                "STRESS_SL_PCT": "5%",
                "CALM_SL_PCT": "5",
            },
        ):
            self.assertEqual(get_target_pct_for_regime("stress"), 0.03)
            self.assertEqual(get_target_pct_for_regime("calm"), 0.05)
            self.assertEqual(target_pcts_for_regime("stress"), (0.03, None))
            self.assertEqual(target_pcts_for_regime("calm"), (0.05, None))
            self.assertEqual(get_sl_pct_for_regime("stress"), 0.05)
            self.assertEqual(get_sl_pct_for_regime("calm"), 0.05)

    def test_regime_target_percentage_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_target_pct_for_regime("stress"), 0.10)
            self.assertEqual(get_target_pct_for_regime("calm"), 0.07)
            self.assertEqual(target_pcts_for_regime("stress"), (0.10, None))
            self.assertEqual(target_pcts_for_regime("calm"), (0.07, None))
            self.assertEqual(get_sl_pct_for_regime("stress"), 0.05)
            self.assertEqual(get_sl_pct_for_regime("calm"), 0.03)

    def test_legacy_target_1_env_names_still_work_as_fallbacks(self) -> None:
        with patch.dict(
            "os.environ",
            {"STRESS_TARGET_1_PCT": "4", "CALM_TARGET_1_PCT": "6"},
            clear=True,
        ):
            self.assertEqual(get_target_pct_for_regime("stress"), 0.04)
            self.assertEqual(get_target_pct_for_regime("calm"), 0.06)

    def test_normalize_pct_accepts_decimal_and_whole_percent(self) -> None:
        self.assertEqual(normalize_pct(0.05), 0.05)
        self.assertEqual(normalize_pct(5), 0.05)

    def test_single_target_hit_rachets_instead_of_closing(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(resolve_exit_reason(trade, 121.0, now, time(15, 15)), "TARGET_HIT")

    def test_single_target_and_stop_loss_ratchet_math(self) -> None:
        entry = 409.4
        target_pct = 0.05
        stop_loss_pct = 0.05
        self.assertAlmostEqual(entry * (1 + target_pct), 429.87, places=2)
        self.assertAlmostEqual(entry * (1 - stop_loss_pct), 388.93, places=2)

        ratchet_price = 433.6
        self.assertAlmostEqual(ratchet_price * (1 + target_pct), 455.28, places=2)
        self.assertAlmostEqual(ratchet_price * (1 - stop_loss_pct), 411.92, places=2)

    def test_stop_loss_exit(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(resolve_exit_reason(trade, 89.0, now, time(15, 15)), "STOP_LOSS_HIT")

    def test_final_trading_day_exit_at_market_close(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 15, 16, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(
            resolve_exit_reason(
                trade, 100.0, now, time(15, 15),
                max_open_days=5, trading_days_open=5,
            ),
            "MAX_TRADING_DAYS_EXIT",
        )

    def test_position_stays_open_before_final_trading_day(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 15, 16, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertIsNone(
            resolve_exit_reason(
                trade, 100.0, now, time(15, 15),
                max_open_days=5, trading_days_open=4,
            )
        )

    def test_final_day_exit_waits_until_close(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertIsNone(
            resolve_exit_reason(
                trade, 100.0, now, time(15, 15),
                max_open_days=5, trading_days_open=5,
            )
        )


if __name__ == "__main__":
    unittest.main()
