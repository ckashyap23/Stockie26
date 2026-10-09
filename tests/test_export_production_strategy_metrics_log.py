from datetime import date

import pandas as pd

from scripts.Common import export_production_strategy_metrics_log as metrics_log


class _Registry:
    @staticmethod
    def get_meta(name):
        strategy_type = "RESEARCH" if name == "ResearchOnly" else "SIGNAL"
        return type("Meta", (), {"strategy_type": strategy_type})()


def test_metrics_log_keeps_all_dates_and_binary_production_fires(monkeypatch):
    base = pd.DataFrame(
        {
            "signal_date": ["2024-01-01", "2024-01-02"],
            "rsi14": [40.0, 60.0],
            "next_open": [101.0, 102.0],
            "future_high_nd": [105.0, 106.0],
            "global_us_return_mean": [0.01, -0.01],
            "actual_trade_label": ["CALL", "PUT"],
            "actual_trade_label_relaxed": ["CALL", "BOTH"],
        }
    )
    signals = {
        "CallSignal": pd.Series(["CALL", "NO_POSITION"]),
        "PutSignal": pd.Series(["NO_POSITION", "PUT"]),
        "ResearchOnly": pd.Series(["CALL", "CALL"]),
    }
    monkeypatch.setattr(metrics_log, "build_base", lambda: base)
    monkeypatch.setattr(metrics_log, "gather_signals", lambda *_: signals)
    monkeypatch.setattr(metrics_log, "get_strategy_registry", lambda: _Registry())
    monkeypatch.setattr(metrics_log, "_add_raw_global_columns", lambda frame: frame)

    result = metrics_log.build_production_strategy_metrics_log(
        date(2024, 1, 1),
        date(2024, 1, 2),
    )

    assert len(result) == 2
    assert result["strategy_CallSignal_fired"].tolist() == [1, 0]
    assert result["strategy_PutSignal_fired"].tolist() == [0, 1]
    assert "strategy_ResearchOnly_fired" not in result
    assert "next_open" not in result
    assert "future_high_nd" not in result
    assert result.columns[-2:].tolist() == [
        "actual_trade_label",
        "actual_trade_label_relaxed",
    ]


def test_metrics_log_validates_date_range():
    try:
        metrics_log.build_production_strategy_metrics_log(
            date(2024, 1, 2),
            date(2024, 1, 1),
        )
    except ValueError as exc:
        assert str(exc) == "start must be on or before end"
    else:
        raise AssertionError("Expected invalid date range to raise ValueError")
