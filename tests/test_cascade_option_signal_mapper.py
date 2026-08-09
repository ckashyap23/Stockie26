from __future__ import annotations

import os

import pandas as pd
import pytest

from src.technical_analysis.cascade.dataset import build_base
from src.technical_analysis.cascade.engine import gather_regime_signals
from src.technical_analysis.cascade.option_signal_mapper import enrich_option_signal_columns, _strength_config
from src.technical_analysis.cascade.strategies import PROMOTED_REGIME_FAMILIES


def test_enrich_option_signal_columns_uses_final_prediction_as_direction() -> None:
    df = pd.DataFrame(
        {
            "trade_date": ["2026-06-24", "2026-06-25"],
            "regime": ["stress", "stress"],
            "resistance_distance_10d": [0.02, 0.02],
            "support_distance_10d": [0.02, 0.02],
        }
    )
    final_prediction = pd.Series(["CALL", "NO_POSITION"], index=df.index)
    regime_signals = {
        "stress": {
            "MomentumDirectional_ContextVotes_StrongExpansionGuard": pd.Series(["CALL", "NO_POSITION"], index=df.index)
        }
    }
    eligibility = {
        "stress": ({"MomentumDirectional_ContextVotes_StrongExpansionGuard": 0.857}, {})
    }

    enriched = enrich_option_signal_columns(df, final_prediction, regime_signals, eligibility)

    assert "raw_signal" not in enriched.columns
    assert "setup_type" not in enriched.columns
    assert enriched.loc[0, "direction"] == "CALL"
    assert pd.isna(enriched.loc[0, "stock_regime"])
    assert enriched.loc[0, "primary_strategy"] == "MomentumDirectional_ContextVotes_StrongExpansionGuard"
    assert enriched.loc[0, "signal_style"] == "trend_momentum"
    assert enriched.loc[0, "strength_label"] == "STRONG"
    assert enriched.loc[0, "strength_score"] == 83.0
    assert enriched.loc[0, "confidence_level"] == 0.857
    assert enriched.loc[1, "direction"] == "NO_POSITION"
    assert pd.isna(enriched.loc[1, "strength_score"])
    assert pd.isna(enriched.loc[0, "expected_move_pct"])
    assert pd.isna(enriched.loc[0, "is_option_eligible"])
    assert pd.isna(enriched.loc[0, "option_bias"])
    assert pd.isna(enriched.loc[0, "conflict_flag"])


def test_strength_config_covers_promoted_production_strategies() -> None:
    if os.getenv("RUN_DB_TESTS") != "1":
        pytest.skip("requires Supabase feature rows; set RUN_DB_TESTS=1")
    emitted = {
        name
        for regime_signals in gather_regime_signals(build_base(), PROMOTED_REGIME_FAMILIES).values()
        for name in regime_signals
    }
    configured = set(_strength_config()["strategies"])

    assert emitted <= configured

