from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.execution.paper import (
    LiveQuote,
    capture_paper_order_charges,
    fetch_live_option_quote,
    monitor_open_paper_trades,
    resolve_exit_reason,
)
from src.common.config import (
    get_probe_target_pct,
    get_sl_pct,
    get_sl_pct_for_strategy,
    get_sl_pct_probe,
    get_target_pct,
    get_target_pct_for_strategy,
    normalize_pct,
)
from src.technical_analysis.optionselection.pipeline import default_target_pcts


class PaperExecutionTests(unittest.TestCase):
    def test_live_quote_uses_depth_snapshot_time_before_last_trade_time(self) -> None:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        last_trade_time = timestamp - timedelta(minutes=1)
        kite = Mock()
        kite.fetch_quote_bulk.return_value = {
            "NFO:NIFTY26JUL23500CE": {
                "last_price": 100.0,
                "timestamp": timestamp,
                "last_trade_time": last_trade_time,
                "depth": {
                    "buy": [{"price": 99.5, "quantity": 65}],
                    "sell": [{"price": 100.5, "quantity": 65}],
                },
            }
        }

        quote = fetch_live_option_quote(kite, "NIFTY26JUL23500CE")

        self.assertEqual(quote.quote_time.astimezone(timezone.utc), timestamp)

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

    def test_target_percentages(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TARGET_PCT_EFFECTIVE": "3%",
                "SL_PCT_EFFECTIVE": "5%",
            },
            clear=True,
        ):
            self.assertEqual(get_target_pct(), 0.03)
            self.assertEqual(default_target_pcts(), (0.03, None))
            self.assertEqual(get_sl_pct(), 0.05)

    def test_target_percentage_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                get_target_pct()
            with self.assertRaises(RuntimeError):
                get_sl_pct()

    def test_target_pct_fallback_name_still_works(self) -> None:
        with patch.dict(
            "os.environ",
            {"TARGET_PCT": "4"},
            clear=True,
        ):
            self.assertEqual(get_target_pct(), 0.04)

    def test_all_strategies_use_shared_target_percentage(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TARGET_PCT_EFFECTIVE": "0.05",
                "SL_PCT_EFFECTIVE": "0.05",
            },
            clear=True,
        ):
            self.assertEqual(get_target_pct(), 0.05)
            self.assertEqual(get_probe_target_pct(), 0.05)
            self.assertEqual(get_target_pct_for_strategy("DRIFT_PROBE_PUT"), 0.05)
            self.assertEqual(get_target_pct_for_strategy("PullbackCall_TrendIntact"), 0.05)
            self.assertEqual(get_sl_pct(), 0.05)
            self.assertEqual(get_sl_pct_probe(), 0.05)
            self.assertEqual(get_sl_pct_for_strategy("DRIFT_PROBE_PUT"), 0.05)

    def test_normalize_pct_accepts_decimal_and_whole_percent(self) -> None:
        self.assertEqual(normalize_pct(0.05), 0.05)
        self.assertEqual(normalize_pct(5), 0.05)

    def test_target_hit_closes_oco_position(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        self.assertEqual(resolve_exit_reason(trade, 121.0), "TARGET_HIT")

    def test_stop_loss_exit(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        self.assertEqual(resolve_exit_reason(trade, 89.0), "STOP_LOSS_HIT")

    def test_position_has_no_time_exit_when_paper_force_exit_is_disabled(self) -> None:
        trade = {"target_1_price": 110.0, "stop_loss_price": 90.0}
        self.assertIsNone(resolve_exit_reason(trade, 100.0))

    @patch("src.execution.paper.fetch_live_option_quote")
    @patch("src.execution.paper.KiteClient")
    @patch("src.execution.paper.get_database_client")
    @patch("src.execution.paper.get_settings")
    def test_target_touch_advances_cascade_without_closing(
        self,
        get_settings_mock,
        get_database_client_mock,
        kite_client_mock,
        fetch_quote_mock,
    ) -> None:
        db = Mock()
        get_database_client_mock.return_value = db
        db.list_open_paper_trades.return_value = [{
            "id": 7,
            "option_symbol": "NIFTY26JUL23500CE",
            "entry_price": 100.0,
            "lot_size": 65,
            "quantity": 65,
            "target_1_price": 110.0,
            "stop_loss_price": 90.0,
        }]
        fetch_quote_mock.return_value = LiveQuote(
            symbol="NFO:NIFTY26JUL23500CE",
            last_price=111.0,
            quote_time=datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc),
            best_bid_price=111.0,
            best_bid_quantity=65,
            best_ask_price=112.0,
            best_ask_quantity=65,
            raw={},
        )
        db.ratchet_paper_trade_targets.return_value = SimpleNamespace(
            target_price=120.0,
            stop_loss_price=99.0,
        )

        result = monitor_open_paper_trades()

        self.assertEqual(result["cascaded"], 1)
        self.assertEqual(result["closed"], 0)
        db.trigger_paper_oco_target.assert_called_once()
        db.create_paper_oco_orders.assert_called_once_with(
            signal_id=7,
            quantity=65,
            target_price=120.0,
            stop_loss_price=99.0,
        )
        db.close_paper_trade.assert_not_called()
        db.fill_paper_oco_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
