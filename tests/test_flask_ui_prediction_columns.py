from datetime import date
from types import SimpleNamespace

import pandas as pd
import flask_app

from flask_app import (
    PRODUCTION_DEFAULT_START,
    app,
    format_signal_row,
    prepare_ui_dataframe,
    production_default_end,
    research_controls,
)


def test_production_date_defaults_cover_2026_through_latest_date():
    assert PRODUCTION_DEFAULT_START == date(2026, 1, 1)
    assert production_default_end() == date.today()


def test_research_grid_defaults_target_to_five_percent():
    with app.test_request_context():
        controls = research_controls()
    assert 'name="target_pct" value="0.05" checked' in controls
    assert 'name="target_pct" value="0.01" checked' not in controls


def test_research_grid_stop_loss_options_default_to_two_percent():
    with app.test_request_context():
        controls = research_controls()
    assert 'name="stop_loss_pct" value="0.02" checked' in controls
    assert 'name="stop_loss_pct" value="0.01" checked' not in controls
    assert 'name="stop_loss_pct" value="0.03" checked' not in controls
    assert 'name="stop_loss_pct" value="0.05" checked' not in controls
    assert "No stop loss" not in controls
    assert 'name="stop_loss_pct" value="0.1"' not in controls


def test_analyze_misses_runs_script_and_returns_two_downloads(tmp_path, monkeypatch):
    precision = tmp_path / "NIFTY_stress_in_sample_precision_misses.csv"
    recall = tmp_path / "NIFTY_stress_in_sample_recall_misses.csv"
    precision.write_text("signal_date\n", encoding="utf-8")
    recall.write_text("signal_date\n", encoding="utf-8")
    monkeypatch.setattr(flask_app, "MISS_ANALYSIS_FILES", {
        "precision": precision,
        "recall": recall,
    })
    calls = []
    monkeypatch.setattr("subprocess.run", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr="")
    ))

    response = app.test_client().post("/production/analyze-misses")

    assert response.status_code == 200
    assert calls[0][0][1] == "scripts/Common/analyze_precision_misses.py"
    assert response.get_json()["downloads"] == [
        "/production/analyze-misses/download/precision",
        "/production/analyze-misses/download/recall",
    ]
    download = app.test_client().get("/production/analyze-misses/download/precision")
    assert download.status_code == 200
    assert "attachment" in download.headers["Content-Disposition"]
    assert precision.name in download.headers["Content-Disposition"]


def test_ui_uses_effective_prediction_and_hides_promotion_audit_columns():
    source = pd.DataFrame({
        "signal_date": ["2026-07-01"],
        "predicted": ["NO_POSITION"],
        "effective_prediction": ["CALL"],
        "watch_signal": ["CALL_3D_WATCH"],
        "promoted_prediction": ["CALL"],
        "promotion_reason": ["PROMOTED_BY_SAME_FAMILY"],
        "strategy_family": ["ExampleCall"],
        "strategy_type": ["WATCH_ONLY"],
        "strategy_authority": ["WATCH_ONLY"],
        "watch_family": ["ExampleCall"],
        "confirming_variant": ["ExampleCall_HighPrecision"],
        "family_confirmation_match": [True],
        "promotion_block_reason": [None],
        "selected_strategy": ["LONG_CALL"],
    })

    displayed = prepare_ui_dataframe(source)

    assert displayed.loc[0, "Predicted"] == "CALL"
    assert displayed.loc[0, "selected_strategy"] == "LONG_CALL"
    assert not set(displayed.columns).intersection({
        "effective_prediction", "watch_signal", "promoted_prediction",
        "promotion_reason", "strategy_family", "strategy_type",
        "strategy_authority", "watch_family", "confirming_variant",
        "family_confirmation_match", "promotion_block_reason",
    })


def test_ui_creates_predicted_column_when_only_effective_value_exists():
    displayed = prepare_ui_dataframe(pd.DataFrame({
        "effective_prediction": ["PUT"],
        "final_prediction": ["NO_POSITION"],
    }))

    assert displayed.columns.tolist() == ["Predicted"]
    assert displayed.loc[0, "Predicted"] == "PUT"


def test_research_leaderboard_has_strategy_type_then_family_filters():
    response = app.test_client().get("/research")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    type_filter_at = page.index('id="leaderboard-type-filter"')
    family_filter_at = page.index('id="leaderboard-family-filter"')
    assert type_filter_at < family_filter_at
    assert "strategy_family" in page
    assert "strategy_type" in page
    assert "OversoldBounceCall" in page
    assert "⭐" not in page
    assert 'title="Production strategy: can directly generate trades and can create or confirm watches."' in page
    assert 'title="Production strategy: can create or confirm watches, but cannot directly generate a trade without promotion."' in page
    assert 'title="Research-grid only: excluded from production trading and watch/promotion logic."' in page
    assert page.index("<th>qualityBased_F1</th>") < page.index("<th>watch_promotions</th>")


def test_production_row_exposes_originating_watch_strategy():
    displayed = format_signal_row({
        "effective_prediction": "CALL",
        "prediction_strategy": "OversoldBounceCall_HighPrecision",
        "watch_variant": "OversoldBounceCall_MoreTrades",
        "watch_strategy_type": "WATCH_ONLY",
    })

    assert displayed["prediction_strategy"] == "OversoldBounceCall_HighPrecision"
    assert displayed["watched_strategy"] == "OversoldBounceCall_MoreTrades"
    assert "watched_strategy_type" not in displayed


def test_trades_page_omits_redundant_daily_paper_table(monkeypatch):
    monkeypatch.setattr(flask_app, "load_latest_option_next_trade_date", lambda: date(2026, 7, 7))
    monkeypatch.setattr(flask_app, "load_live_executed_trades", lambda: (pd.DataFrame(), None))

    response = app.test_client().get("/trades")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Paper Trades For 2026-07-07" not in page
    assert "Executed Paper Trades" in page
    assert "Closed Paper Trades" in page
    assert "Open Paper Trades" in page
    assert "VectorBT Trade Replay" in page
