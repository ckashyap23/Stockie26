from backtest.vectorbt_research.strategy_grid import RESEARCH_VARIANTS
from src.technical_analysis.strategy_registry import get_strategy_registry

def test_every_research_variant_has_strategy_metadata():
    registry = get_strategy_registry()
    registry.validate_complete(variant.name for variant in RESEARCH_VARIANTS)


def test_research_grid_only_contains_approved_research_variants():
    assert {variant.name for variant in RESEARCH_VARIANTS} == {
        "PullbackCall_DeepWashout",
        "DeclineContinuationPut_ATR",
        "BreakdownPut_20d",
        "RsiReversion_6040",
        "DRIFT_PROBE_PUT",
        "BollingerMeanReversion",
        "MacroDirection",
    }


def test_macro_direction_is_research_only():
    assert get_strategy_registry().get_meta("MacroDirection").strategy_type == "RESEARCH"


def test_no_active_strategy_level_global_guards():
    registry = get_strategy_registry()
    active_names = {variant.name for variant in RESEARCH_VARIANTS} | set(registry.variants)
    assert not any("_Global" in name for name in active_names)


def test_bollinger_watch_variants_are_removed():
    registry = get_strategy_registry()
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
    registry = get_strategy_registry()
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
