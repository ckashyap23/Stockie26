from __future__ import annotations

import pandas as pd

from backtest.vectorbt_research.strategy_grid import (
    CALL,
    FLAT,
    PUT,
    call_intent_metrics,
    load_strategy_intent_thresholds,
)


def _base_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "signal_date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "actual_trade_label": ["CALL", "BOTH", "PUT"],
            "next_open": [100.0, 100.0, 100.0],
            "next_high": [102.0, 100.2, 101.0],
            "next_return_pct": [0.01, 0.002, -0.01],
            "ret_2d_prior": [-0.01, -0.02, -0.03],
            "support_distance_10d": [0.01, 0.015, 0.05],
            "near_validated_support_10d": [True, True, False],
            "swing_structure_state": [
                "downtrend_intact",
                "downtrend_intact",
                "uptrend_intact",
            ],
            "future_high_nd": [102.0, 100.8, 100.5],
        }
    )


def test_oversold_intent_metrics_hits_and_misses() -> None:
    eligible = _base_df()
    signal = pd.Series([CALL, FLAT, CALL])
    thresholds = load_strategy_intent_thresholds()

    metrics, diag = call_intent_metrics(
        "ReversalCall",
        eligible,
        signal,
        thresholds,
    )

    assert metrics["intent_fires"] == 2
    assert metrics["intent_hits"] == 1
    assert metrics["intent_F1"] == 0.5
    assert not diag.empty


def test_short_covering_requires_downtrend_context() -> None:
    eligible = _base_df()
    eligible.loc[1, "next_return_pct"] = -0.001
    eligible.loc[1, "future_high_nd"] = 100.2
    signal = pd.Series([CALL, CALL, FLAT])
    thresholds = load_strategy_intent_thresholds()

    metrics, _ = call_intent_metrics(
        "DowntrendBounceCall",
        eligible,
        signal,
        thresholds,
    )

    assert metrics["intent_hits"] == 1
    assert metrics["intent_extra_fires"] == 1


def test_put_family_intent_metrics_are_computed() -> None:
    eligible = _base_df()
    eligible["next_low"] = [99.0, 99.4, 97.0]
    eligible["future_low_nd"] = [99.0, 99.3, 97.0]
    eligible["resistance_distance_10d"] = [0.01, 0.015, 0.03]
    eligible["near_validated_resistance_10d"] = [True, True, False]
    signal = pd.Series([PUT, PUT, FLAT])
    thresholds = load_strategy_intent_thresholds()

    metrics, diag = call_intent_metrics(
        "ReversalPut",
        eligible,
        signal,
        thresholds,
    )

    assert metrics["intent_fires"] == 2
    assert not diag.empty
