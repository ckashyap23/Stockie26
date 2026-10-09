import pandas as pd

from scripts.Common.export_options_trading_log import add_option_movement_metrics


def test_add_option_movement_metrics_uses_entry_as_excursion_anchor() -> None:
    frame = pd.DataFrame(
        [
            {
                "entry_price": 100.0,
                "exit_price": 108.0,
                "target_1_price": 115.0,
                "stop_loss_price": 92.0,
                "option_open": 98.0,
                "option_high": 120.0,
                "option_low": 90.0,
                "option_close": 108.0,
            }
        ]
    )

    result = add_option_movement_metrics(frame).iloc[0]

    assert result["option_max_up_points"] == 20.0
    assert result["option_max_up_pct"] == 0.20
    assert result["option_max_down_points"] == 10.0
    assert result["option_max_down_pct"] == 0.10
    assert result["option_close_move_points"] == 8.0
    assert result["option_day_range_points"] == 30.0
    assert result["target_touched_by_day_high"] == 1
    assert result["stop_touched_by_day_low"] == 1
    assert result["both_exit_levels_touched"] == 1


def test_add_option_movement_metrics_preserves_missing_ohlc() -> None:
    frame = pd.DataFrame(
        [
            {
                "entry_price": 100.0,
                "exit_price": None,
                "target_1_price": 115.0,
                "stop_loss_price": 92.0,
                "option_open": None,
                "option_high": None,
                "option_low": None,
                "option_close": None,
            }
        ]
    )

    result = add_option_movement_metrics(frame).iloc[0]

    assert pd.isna(result["option_max_up_points"])
    assert pd.isna(result["option_max_down_points"])
    assert result["target_touched_by_day_high"] == 0
    assert result["stop_touched_by_day_low"] == 0


def test_add_option_movement_metrics_does_not_report_negative_excursions() -> None:
    frame = pd.DataFrame(
        [
            {
                "entry_price": 100.0,
                "exit_price": 95.0,
                "target_1_price": 115.0,
                "stop_loss_price": 90.0,
                "option_open": 98.0,
                "option_high": 99.0,
                "option_low": 92.0,
                "option_close": 95.0,
            }
        ]
    )

    result = add_option_movement_metrics(frame).iloc[0]

    assert result["option_max_up_points"] == 0
    assert result["option_max_up_pct"] == 0
    assert result["option_max_down_points"] == 8
