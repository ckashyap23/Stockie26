from datetime import date
from types import SimpleNamespace

import pandas as pd
import flask_app

from flask_app import (
    PRODUCTION_DISPLAY_START,
    app,
    format_signal_row,
    load_strategy_definition_map,
    prepare_ui_dataframe,
    production_default_end,
    research_controls,
    research_predictions_table,
)


def test_production_prediction_display_defaults_to_2026_through_latest_date():
    assert PRODUCTION_DISPLAY_START == date(2026, 1, 1)
    assert production_default_end() == date.today()


def test_research_grid_has_no_option_target_controls():
    with app.test_request_context():
        controls = research_controls()
    assert 'name="target_pct"' not in controls
    assert 'id="leaderboard-family-filter"' not in controls
    assert 'id="leaderboard-type-filter"' not in controls
    assert 'name="variants"' not in controls
    assert "Variant run selection" not in controls
    assert "PullbackCall_TrendIntact" not in controls
    assert "DRIFT_PROBE" not in controls
    assert 'name="start"' not in controls
    assert 'name="end"' not in controls
    assert "Evaluation policy" not in controls


def test_research_grid_has_no_option_stop_controls():
    with app.test_request_context():
        controls = research_controls()
    assert 'name="stop_loss_pct"' not in controls
    assert "No stop loss" not in controls


def test_analyze_misses_runs_script_and_returns_two_downloads(tmp_path, monkeypatch):
    precision = tmp_path / "NIFTY_in_sample_precision_misses.csv"
    recall = tmp_path / "NIFTY_in_sample_recall_misses.csv"
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


def test_ui_uses_effective_prediction_and_hides_internal_audit_columns():
    source = pd.DataFrame({
        "signal_date": ["2026-07-01"],
        "predicted": ["NO_POSITION"],
        "effective_prediction": ["CALL"],
        "strategy_family": ["ExampleCall"],
        "strategy_type": ["SIGNAL"],
        "strategy_authority": ["SIGNAL"],
        "selected_strategy": ["LONG_CALL"],
    })

    displayed = prepare_ui_dataframe(source)

    assert displayed.loc[0, "Predicted"] == "CALL"
    assert displayed.loc[0, "selected_strategy"] == "LONG_CALL"
    assert not set(displayed.columns).intersection({
        "effective_prediction", "strategy_family", "strategy_type", "strategy_authority",
    })


def test_ui_creates_predicted_column_when_only_effective_value_exists():
    displayed = prepare_ui_dataframe(pd.DataFrame({
        "effective_prediction": ["PUT"],
        "final_prediction": ["NO_POSITION"],
    }))

    assert displayed.columns.tolist() == ["Predicted"]
    assert displayed.loc[0, "Predicted"] == "PUT"


def test_research_leaderboard_has_no_family_or_variant_controls():
    response = app.test_client().get("/research")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="leaderboard-family-filter"' not in page
    assert 'id="leaderboard-type-filter"' not in page
    assert "Variant run selection" not in page
    assert "strategy_family" not in page
    assert "strategy_type" in page
    assert "MacroDirection" in page
    assert "â­" not in page
    assert "<th>Quality F1</th>" in page
    assert "<th>watch_promotions</th>" not in page
    assert "<th>watch_promotion_precision</th>" not in page
    assert "<th>watch_promotion_recall</th>" not in page


def test_research_leaderboard_uses_current_strategy_metadata(tmp_path, monkeypatch):
    output_dir = tmp_path / "research"
    output_dir.mkdir()
    monkeypatch.setattr(flask_app, "RESEARCH_OUTPUT_DIR", output_dir)
    monkeypatch.setattr(flask_app, "RESEARCH_OUTPUT_FILES", {
        **flask_app.RESEARCH_OUTPUT_FILES,
        "leaderboard": "strategy_grid_leaderboard.csv",
    })
    pd.DataFrame([
        {
                "strategy_variant": "MacroDirection",
                "strategy_type": "SIGNAL",
            "target_pct": 0.05,
            "trades": 1,
        },
        {
                "strategy_variant": "OldRemovedVariant",
            "strategy_type": "TRADE_ELIGIBLE",
            "target_pct": 0.05,
            "trades": 0,
        },
    ]).to_csv(output_dir / "strategy_grid_leaderboard.csv", index=False)

    response = app.test_client().get("/research")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "MacroDirection" in page
    assert "OldRemovedVariant" not in page
    assert "RESEARCH" in page


def test_research_predictions_table_keeps_fire_context_columns(tmp_path):
    path = tmp_path / "strategy_grid_predictions.csv"
    pd.DataFrame([{
        "strategy_variant": "MacroDirection",
        "strategy_type": "RESEARCH",
        "signal_date": "2026-07-09",
        "trade_date": "2026-07-10",
        "predicted": "CALL",
        "max_underlying_up": 0.015,
        "max_underlying_down": 0.006,
        "us_ret": 0.0123,
        "europe_ret": -0.004,
        "asia_ret": 0.0,
        "actual_label": "CALL",
        "quality_label": "CALL",
    }]).to_csv(path, index=False)

    table = research_predictions_table(path)

    assert table.title == "Predictions"
    assert 'id="research-predictions-table"' in table.html
    assert "<th>signal_date</th>" not in table.html
    assert table.html.index("<th>trade_date</th>") < table.html.index("<th>strategy_variant</th>")
    assert table.html.index("<th>predicted</th>") < table.html.index("<th>actual_label</th>")
    assert table.html.index("<th>max_underlying_down</th>") < table.html.index("<th>us_ret</th>")
    assert "<th>strategy_family</th>" not in table.html
    assert "<th>strategy_type</th>" in table.html
    assert "<th>actual_label</th>" in table.html
    assert "<th>quality_label</th>" in table.html
    assert "<td>1.23%</td>" in table.html
    assert "precision" not in table.html


def test_production_row_exposes_prediction_strategy_only():
    displayed = format_signal_row({
        "effective_prediction": "CALL",
        "prediction_strategy": "MomentumDirectional_ContextVotes_StrongExpansionGuard",
        "strength_score": 80,
        "confidence_level": 0.72,
        "max_underlying_up": 1.5,
        "global_us_return_mean": 0.01,
    })

    assert displayed["prediction_strategy"] == "MomentumDirectional_ContextVotes_StrongExpansionGuard"
    assert "global_index_risk" not in displayed
    assert "watched_strategy" not in displayed
    assert "strength" not in displayed
    assert "confidence" not in displayed
    assert "option_symbol" not in displayed
    assert "target_1" not in displayed
    assert "pnl_status" not in displayed
    assert "signal_date" not in displayed
    keys = list(displayed)
    assert keys.index("max_underlying_up") < keys.index("us_ret")


def test_production_filter_form_targets_table_fragment(monkeypatch):
    monkeypatch.setattr(
        flask_app,
        "load_production_signal_rows",
        lambda start, end: ([{
            "signal_date": "2026-07-01",
            "predicted": "CALL",
            "effective_prediction": "CALL",
        }], ""),
    )
    monkeypatch.setattr(flask_app, "load_global_index_window_rows", lambda: ([], ""))
    monkeypatch.setattr(flask_app, "build_production_roster_table", lambda: flask_app.PageTable(
        title="Production Strategies",
        path=None,
        html="",
        rows=0,
    ))

    response = app.test_client().get("/production?predicted=CALL")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="production-signal-table-card"' in page
    assert 'class="control-grid production-filter-form"' in page
    assert 'data-fragment-url="/production/table"' in page
    assert "/production/table" in page


def test_production_table_fragment_returns_only_signal_table(monkeypatch):
    seen = {}

    def fake_load(start, end):
        seen["start"] = start
        seen["end"] = end
        return ([{
            "signal_date": "2026-07-01",
            "predicted": "CALL",
            "effective_prediction": "CALL",
        }, {
            "signal_date": "2026-07-02",
            "predicted": "PUT",
            "effective_prediction": "PUT",
        }], "")

    monkeypatch.setattr(flask_app, "load_production_signal_rows", fake_load)

    response = app.test_client().get(
        "/production/table?start=2026-07-01&end=2026-07-15&predicted=CALL"
    )

    assert response.status_code == 200
    fragment = response.get_data(as_text=True)
    assert seen == {"start": date(2026, 7, 1), "end": date(2026, 7, 15)}
    assert fragment.strip().startswith('<section class="table-card" id="production-signal-table-card">')
    assert "<!doctype html>" not in fragment
    assert "<td>CALL</td>" in fragment
    assert "<td>PUT</td>" not in fragment
    assert "(1 rows)" in fragment


def test_strategy_definition_map_loads_canonical_definition(tmp_path, monkeypatch):
    definitions = tmp_path / "strategy_definitions.csv"
    definitions.write_text(
        "record_type,name,definition\n"
        "variant,CustomResearch,Helpful hover definition.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(flask_app, "STRATEGY_DEFINITION_PATHS", (definitions,))

    tooltip = load_strategy_definition_map()["CustomResearch"]
    assert "Regime:" not in tooltip
    assert "Family:" not in tooltip
    assert "Helpful hover definition." in tooltip


def test_strategy_definition_map_includes_production_strategy_definitions():
    definitions = load_strategy_definition_map()

    tooltip = definitions["ReversalCall"]
    assert "Family:" not in tooltip
    assert "Type: SIGNAL" in tooltip
    assert "rsi5" in tooltip


def test_strategy_definition_map_marks_macro_direction_as_research():
    definitions = load_strategy_definition_map()

    tooltip = definitions["MacroDirection"]
    assert "Family:" not in tooltip
    assert "Type: RESEARCH" in tooltip
    assert "macro_risk_off" in tooltip


def test_strategy_definition_map_excludes_strategy_level_global_guard_variants():
    definitions = load_strategy_definition_map()

    assert not any("_Global" in name for name in definitions)


def test_dashboard_includes_global_strategy_definition_tooltip_map(tmp_path, monkeypatch):
    definitions = tmp_path / "strategy_definitions.csv"
    definitions.write_text(
        "record_type,name,family,definition\n"
            "variant,ExpansionVotes_Strong,ExampleFamily,Helpful hover definition.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(flask_app, "STRATEGY_DEFINITION_PATHS", (definitions,))

    with app.test_request_context():
        page = flask_app.render_dashboard(
            active="trades",
            title="Trades",
            subtitle="",
            controls="",
            tables=[],
            summary="",
            summary_title="",
        )

    assert "Regime:" not in page
    assert "ExpansionVotes_Strong" in page
    assert "predictionstrategy" in page
    assert "watchedstrategy" not in page


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
