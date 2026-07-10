from types import SimpleNamespace

from src.technical_analysis.optionselection.pipeline import (
    option_selection_to_row,
    prediction_to_underlying_view,
)


def _prediction() -> dict:
    return {
        "signal_date": "2026-07-01",
        "next_trade_date": "2026-07-02",
        "final_prediction": "CALL",
        "regime": "stress",
        "strength_score": 80,
    }


def test_prediction_view_uses_migrated_signal_date() -> None:
    view = prediction_to_underlying_view(_prediction(), "NIFTY")
    assert view.trade_date == "2026-07-01"


def test_option_selection_row_uses_migrated_signal_date(monkeypatch) -> None:
    monkeypatch.setenv("STRESS_SL_PCT", "0.03")
    candidate = SimpleNamespace(
        legs=[],
        strategy_type="NO_TRADE",
        direction="NEUTRAL",
        entry_debit_or_credit=None,
        max_profit=None,
        max_loss=None,
        breakeven=None,
        reward_risk=None,
        score=0,
        confidence="LOW",
        total_delta=None,
        total_gamma=None,
        total_theta=None,
        total_vega=None,
    )
    result = SimpleNamespace(
        selected_strategy=candidate,
        option_bias="NEUTRAL",
        no_trade_reason="test",
        evaluated_candidate_count=0,
    )

    row = option_selection_to_row(
        _prediction(), result, "NIFTY", "cascade_v1", 25_000, "2026-07-01 15:15:00",
        target_pcts=(0.05, 0.07),
    )

    assert row["trade_date"] == "2026-07-01"
    assert row["target_1_pct"] == 0.05
    assert row["target_2_pct"] is None
    assert row["target_2_price"] is None
    assert row["stop_loss_pct"] == 0.03
    assert row["stop_loss_enabled"] is True
