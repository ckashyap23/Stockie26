from __future__ import annotations

import unittest
from datetime import datetime, time
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from src.execution.paper import capture_paper_order_charges, resolve_exit_reason
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
                "STRESS_TARGET_1_PCT": "0.005",
                "STRESS_TARGET_2_PCT": "0.007",
                "CALM_TARGET_1_PCT": "0.003",
                "CALM_TARGET_2_PCT": "0.005",
            },
        ):
            self.assertEqual(target_pcts_for_regime("stress"), (0.005, 0.007))
            self.assertEqual(target_pcts_for_regime("calm"), (0.003, 0.005))

    def test_target_2_takes_priority_after_stop(self) -> None:
        trade = {"target_1_price": 110.0, "target_2_price": 120.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(resolve_exit_reason(trade, 121.0, now, time(15, 15)), "TARGET_2_HIT")

    def test_stop_loss_exit(self) -> None:
        trade = {"target_1_price": 110.0, "target_2_price": 120.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(resolve_exit_reason(trade, 89.0, now, time(15, 15)), "STOP_LOSS_HIT")

    def test_final_trading_day_exit_at_market_close(self) -> None:
        trade = {"target_1_price": 110.0, "target_2_price": 120.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 15, 16, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(
            resolve_exit_reason(
                trade, 100.0, now, time(15, 15),
                max_open_days=5, trading_days_open=5,
            ),
            "MAX_TRADING_DAYS_EXIT",
        )

    def test_position_stays_open_before_final_trading_day(self) -> None:
        trade = {"target_1_price": 110.0, "target_2_price": 120.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 15, 16, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertIsNone(
            resolve_exit_reason(
                trade, 100.0, now, time(15, 15),
                max_open_days=5, trading_days_open=4,
            )
        )

    def test_final_day_exit_waits_until_close(self) -> None:
        trade = {"target_1_price": 110.0, "target_2_price": 120.0, "stop_loss_price": 90.0}
        now = datetime(2026, 6, 29, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertIsNone(
            resolve_exit_reason(
                trade, 100.0, now, time(15, 15),
                max_open_days=5, trading_days_open=5,
            )
        )


if __name__ == "__main__":
    unittest.main()
