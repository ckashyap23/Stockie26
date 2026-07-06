import pandas as pd

from backtest.vectorbt_research.strategy_grid import RESEARCH_VARIANTS, watch_promotion_attribution
from src.technical_analysis.strategy_families import (
    collapse_firing_variants_by_family,
    get_strategy_family_registry,
)
from src.technical_analysis.cascade.constants import CALL, FLAT, PUT
from src.technical_analysis.cascade.strategies import (
    _promoted_range_breakout,
    calm_momentum_call,
    calm_momentum_put,
    mean_reversion,
)


def test_every_research_variant_has_family_metadata():
    registry = get_strategy_family_registry()
    registry.validate_complete(variant.name for variant in RESEARCH_VARIANTS)


def test_family_collapse_keeps_highest_precision_variant():
    reps = collapse_firing_variants_by_family([
        {"strategy_variant": "OversoldBounceCall_MoreTrades", "direction": "CALL", "historical_precision": 0.64},
        {"strategy_variant": "OversoldBounceCall_HighPrecision", "direction": "CALL", "historical_precision": 0.72},
    ])
    assert len(reps) == 1
    assert reps[0]["strategy_variant"] == "OversoldBounceCall_HighPrecision"


def test_range_breakout_put_global_all_disagree_is_put_only_watch():
    registry = get_strategy_family_registry()
    assert not registry.validate_direction("RangeBreakoutPut_GlobalAllDisagree", "CALL")[0]
    assert registry.validate_direction("RangeBreakoutPut_GlobalAllDisagree", "PUT")[0]
    meta = registry.get_meta("RangeBreakoutPut_GlobalAllDisagree")
    assert meta.family == "RangeBreakdownPut"
    assert meta.can_create_watch
    assert not meta.can_hard_trade
    assert not meta.can_confirm_watch
    call_meta = registry.get_meta("RangeBreakoutCall_GlobalRiskAgree")
    assert call_meta.direction == "CALL"
    assert meta.direction == "PUT"


def test_promoted_range_breakout_wrapper_blocks_calls(monkeypatch):
    import src.technical_analysis.cascade.strategies as strategies

    index = [0, 1, 2]
    raw = pd.Series([CALL, PUT, FLAT], index=index)
    monkeypatch.setattr(
        strategies,
        "range_breakout",
        lambda _df: {"strategy_RangeBreakoutPut_GlobalAllDisagree_signal": raw},
    )
    result = _promoted_range_breakout(pd.DataFrame(index=index))
    assert result["strategy_RangeBreakoutPut_GlobalAllDisagree_signal"].tolist() == [
        FLAT, PUT, FLAT,
    ]


def test_new_stress_watch_strategies_cannot_hard_promote():
    registry = get_strategy_family_registry()
    for variant in (
        "StressOverboughtFadePut_HighPrecision",
        "UpMomentumCall_HighPrecision",
    ):
        meta = registry.get_meta(variant)
        assert meta.strategy_type == "WATCH_ONLY"
        assert meta.can_create_watch
        assert not meta.can_hard_trade
        assert not meta.can_confirm_watch


def test_range_breakout_call_is_watch_only():
    meta = get_strategy_family_registry().get_meta("RangeBreakoutCall_GlobalRiskAgree")
    assert meta.strategy_type == "WATCH_ONLY"
    assert meta.can_create_watch
    assert not meta.can_confirm_watch


def test_macd_is_research_only():
    meta = get_strategy_family_registry().get_meta("MACD_EMA5_20")
    assert meta.strategy_type == "RESEARCH"
    assert not meta.can_create_watch
    assert not meta.can_confirm_watch


def test_guard_variant_inherits_parent_policy():
    registry = get_strategy_family_registry()
    meta = registry.get_meta("DownMomentumPut_HighPrecision_GlobalAllDisagree")
    assert meta.strategy_type == "TRADE_ELIGIBLE"
    assert meta.family == "DownMomentumPut"
    assert "DownMomentumPut_HighPrecision_GlobalAllDisagree" not in registry.variants


def test_calm_momentum_call_variants_apply_slope_room_and_asia_rules():
    df = pd.DataFrame({
        "regime": ["calm", "calm", "calm", "calm"],
        "bb_width": [0.040, 0.040, 0.040, 0.0399],
        "ret_3d": [0.003, 0.003, 0.003, 0.003],
        "ma5d_slope": [0.001, 0.001, -0.001, 0.001],
        "ma10d_slope": [-0.001, -0.0011, 0.001, 0.001],
        "range_position_10d": [0.95, 0.90, 0.80, 0.80],
        "global_asia_return_mean": [0.001, 0.001, 0.001, 0.001],
        "volatility_10d": [0.003] * 4,
        "ret_5d": [0.001] * 4,
        "close_1515": [100.0, 101.0, 102.0, 103.0],
    })
    signals = calm_momentum_call(df)
    assert signals["strategy_CalmMomentumCall_Continuation_signal"].tolist() == [CALL, FLAT, FLAT, FLAT]
    assert signals["strategy_CalmMomentumCall_Continuation_GlobalAsiaAgree_signal"].tolist() == [CALL, FLAT, FLAT, FLAT]


def test_calm_momentum_put_requires_negative_slope_and_range_floor():
    df = pd.DataFrame({
        "bb_width": [0.040, 0.040, 0.040, 0.0399],
        "ret_3d": [-0.003, -0.003, -0.003, -0.003],
        "ma5d_slope": [-0.001, 0.001, -0.001, -0.001],
        "range_position_10d": [0.20, 0.50, 0.19, 0.50],
        "regime": ["calm"] * 4,
        "volatility_10d": [0.003] * 4,
        "ret_5d": [-0.001] * 4,
        "close_1515": [103.0, 102.0, 101.0, 100.0],
    })
    signals = calm_momentum_put(df)
    assert signals["strategy_CalmMomentumPut_Continuation_signal"].tolist() == [PUT, FLAT, FLAT, FLAT]


def test_bollinger_mean_reversion_vix_and_strong_trend_guards():
    df = pd.DataFrame({
        "close_1515": [90.0, 90.0, 110.0],
        "bb_lower": [95.0, 95.0, 95.0],
        "bb_upper": [105.0, 105.0, 105.0],
        "vix_close": [11.9, 12.0, 12.0],
        "ma20_slope": [0.0, -0.003, 0.003],
        "trend_efficiency_10d": [0.0, 0.25, 0.25],
        "rsi14": [50.0, 50.0, 50.0],
        "rsi5": [50.0, 50.0, 50.0],
        "bb_width": [0.05, 0.05, 0.05],
    })
    signal = mean_reversion(df)["strategy_BollingerMeanReversion_signal"]
    assert signal.tolist() == [FLAT, FLAT, FLAT]


def test_relaxed_bollinger_variants_are_watch_only():
    registry = get_strategy_family_registry()
    for name in (
        "BollingerMeanReversion_RelaxedVolWatch",
        "BollingerMeanReversion_BorderlineTrendWatch",
        "BollingerMeanReversion_BandProximityWatch",
    ):
        meta = registry.get_meta(name)
        assert meta.strategy_type == "WATCH_ONLY"
        assert meta.can_create_watch
        assert not meta.can_hard_trade


def test_research_grid_attributes_promoted_prediction_to_watch_origin():
    df = pd.DataFrame({
        "signal_date": ["2026-07-01", "2026-07-02"],
        "regime": ["stress", "stress"],
        "close_1515": [100.0, 101.0],
        "actual_trade_label": [FLAT, CALL],
    })
    signal = pd.Series([CALL, FLAT])

    promoted, rows = watch_promotion_attribution(
        df, signal, "BollingerMeanReversion_RelaxedVolWatch"
    )

    assert promoted.tolist() == [FLAT, CALL]
    assert rows.loc[0, "watch_strategy"] == "BollingerMeanReversion_RelaxedVolWatch"
    assert rows.loc[0, "watch_signal_date"] == "2026-07-01"
    assert rows.loc[0, "promotion_signal_date"] == "2026-07-02"
