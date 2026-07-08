import pandas as pd

from backtest.production.pipeline_backtest_pnl import _simulate_exits


def test_actual_fill_drives_initial_levels_and_ignores_pre_entry_snapshots() -> None:
    plans = pd.DataFrame([{
        "trade_id": "2026-07-06_1",
        "trade_date": pd.Timestamp("2026-07-06").date(),
        "replay_trade_date": pd.Timestamp("2026-07-07").date(),
        "primary_buy_entry_price": 530.0,
        "actual_entry_price": 409.4,
        "actual_entry_time": pd.Timestamp("2026-07-07 03:50:33+00:00"),
        "target_1_pct": 0.05,
        "stop_loss_enabled": True,
        "stop_loss_pct": 0.05,
        "final_prediction": "PUT",
        "promoted_prediction": None,
    }])
    snapshots = pd.DataFrame([
        {"trade_id": "2026-07-06_1", "snapshot_time": "2026-07-07 09:15:00", "trade_date": pd.Timestamp("2026-07-07").date(), "price": 433.7, "lot_size": 65},
        {"trade_id": "2026-07-06_1", "snapshot_time": "2026-07-07 09:20:33", "trade_date": pd.Timestamp("2026-07-07").date(), "price": 409.4, "lot_size": 65},
        {"trade_id": "2026-07-06_1", "snapshot_time": "2026-07-07 09:35:12", "trade_date": pd.Timestamp("2026-07-07").date(), "price": 433.6, "lot_size": 65},
        {"trade_id": "2026-07-06_1", "snapshot_time": "2026-07-07 15:15:00", "trade_date": pd.Timestamp("2026-07-07").date(), "price": 418.25, "lot_size": 65},
    ])
    snapshots["snapshot_time"] = pd.to_datetime(snapshots["snapshot_time"])

    result = _simulate_exits(plans, snapshots).iloc[0]

    assert result["entry_price"] == 409.4
    assert result["entry_price_source"] == "actual_paper_fill"
    assert result["entry_snapshot_time"] == pd.Timestamp("2026-07-07 09:20:33")
    assert result["ratchet_count"] == 1
    assert round(result["target_1_price"], 2) == 451.36
    assert round(result["stop_loss_price"], 2) == 406.23
    assert result["exit_reason"] == "TIME_EXIT"
