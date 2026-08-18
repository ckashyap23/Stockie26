import pandas as pd

from backtest.vectorbt_research.strategy_grid import RESEARCH_VARIANTS
from src.technical_analysis.strategy_families import (
    collapse_firing_variants_by_family,
    get_strategy_family_registry,
)
from src.technical_analysis.cascade.constants import PUT

def test_every_research_variant_has_family_metadata():
    registry = get_strategy_family_registry()
    registry.validate_complete(variant.name for variant in RESEARCH_VARIANTS)


def test_research_grid_only_contains_approved_research_variants():
    assert {variant.name for variant in RESEARCH_VARIANTS} == {
        "DeclineContinuationPut_ATR_v2",
        "ExpansionVotes_Strong",
    }


def test_family_collapse_keeps_highest_precision_variant():
    reps = collapse_firing_variants_by_family([
        {"strategy_variant": "DeclineContinuationPut_ATR", "direction": "PUT", "historical_precision": 0.51},
        {"strategy_variant": "DeclineContinuationPut_ATR_v2", "direction": "PUT", "historical_precision": 0.60},
    ])
    assert len(reps) == 1
    assert reps[0]["strategy_variant"] == "DeclineContinuationPut_ATR_v2"


def test_no_active_strategy_level_global_guards():
    registry = get_strategy_family_registry()
    active_names = {variant.name for variant in RESEARCH_VARIANTS} | set(registry.variants)
    assert not any("_Global" in name for name in active_names)


def test_bollinger_watch_variants_are_removed():
    registry = get_strategy_family_registry()
    for name in (
        "BollingerMeanReversion_RelaxedVolWatch",
        "BollingerMeanReversion_BorderlineTrendWatch",
        "BollingerMeanReversion_BandProximityWatch",
    ):
        try:
            registry.get_meta(name)
        except KeyError:
            continue
        raise AssertionError(f"{name} should not remain in strategy metadata")


def test_deleted_calm_fade_variants_are_removed_from_metadata_and_research_grid():
    registry = get_strategy_family_registry()
    names = {variant.name for variant in RESEARCH_VARIANTS}
    for name in (
        "CalmFadePut_Overbought",
        "CalmFadePut_ContextOverbought",
    ):
        assert name not in names
        try:
            registry.get_meta(name)
        except KeyError:
            continue
        raise AssertionError(f"{name} should not remain in strategy metadata")


