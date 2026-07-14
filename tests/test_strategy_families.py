import pandas as pd

from backtest.vectorbt_research.strategy_grid import RESEARCH_VARIANTS, watch_promotion_attribution
from src.technical_analysis.strategy_families import (
    collapse_firing_variants_by_family,
    get_strategy_family_registry,
)
from src.technical_analysis.cascade.constants import CALL, FLAT, PUT
from src.technical_analysis.cascade.engine import build_regime_cascade, gather_regime_signals
from src.technical_analysis.cascade.strategies import (
    PROMOTED_REGIME_FAMILIES,
    WATCH_ONLY_REGIME_FAMILIES,
    _promoted_range_breakout,
    calm_fade_put,
    calm_trend_call,
    calm_momentum_call,
    calm_momentum_put,
    mean_reversion,
    oversold_bounce_call,
)


def test_every_research_variant_has_family_metadata():
    registry = get_strategy_family_registry()
    registry.validate_complete(variant.name for variant in RESEARCH_VARIANTS)


def test_family_collapse_keeps_highest_precision_variant():
    reps = collapse_firing_variants_by_family([
        {"strategy_variant": "DownMomentumPut_MoreTrades", "direction": "PUT", "historical_precision": 0.64},
        {"strategy_variant": "DownMomentumPut_HighPrecision", "direction": "PUT", "historical_precision": 0.72},
    ])
    assert len(reps) == 1
    assert reps[0]["strategy_variant"] == "DownMomentumPut_HighPrecision"


def test_range_breakout_put_is_put_only_watch_without_global_guard():
    registry = get_strategy_family_registry()
    assert not registry.validate_direction("RangeBreakoutPut", "CALL")[0]
    assert registry.validate_direction("RangeBreakoutPut", "PUT")[0]
    meta = registry.get_meta("RangeBreakoutPut")
    assert meta.family == "RangeBreakdownPut"
    assert meta.can_create_watch
    assert not meta.can_hard_trade
    assert not meta.can_confirm_watch
    assert meta.direction == "PUT"


def test_promoted_range_breakout_wrapper_blocks_calls(monkeypatch):
    import src.technical_analysis.cascade.strategies as strategies

    index = [0, 1, 2]
    raw = pd.Series([CALL, PUT, FLAT], index=index)
    monkeypatch.setattr(
        strategies,
        "range_breakout",
        lambda _df: {"strategy_RangeBreakoutPut_signal": raw},
    )
    result = _promoted_range_breakout(pd.DataFrame(index=index))
    assert result["strategy_RangeBreakoutPut_signal"].tolist() == [
        FLAT, PUT, FLAT,
    ]


def test_new_stress_watch_strategies_cannot_hard_promote():
    registry = get_strategy_family_registry()
    for variant in (
        "UpMomentumCall_HighPrecision",
    ):
        meta = registry.get_meta(variant)
        assert meta.strategy_type == "WATCH_ONLY"
        assert meta.can_create_watch
        assert not meta.can_hard_trade
        assert not meta.can_confirm_watch

def test_both_regime_baselines_and_rsi_policy():
    registry = get_strategy_family_registry()
    for variant in ("BollingerMeanReversion", "MACD_EMA5_20"):
        meta = registry.get_meta(variant)
        assert meta.strategy_type == "TRADE_ELIGIBLE"
        assert meta.can_hard_trade
        assert registry.families[meta.family]["regime"] == "all"
    rsi = registry.get_meta("RsiMeanReversion_6040")
    assert rsi.strategy_type == "RESEARCH"
    assert not rsi.can_hard_trade
    assert not rsi.can_create_watch


def test_no_active_strategy_level_global_guards_and_calm_momentum_put_is_trade_eligible():
    registry = get_strategy_family_registry()
    active_names = {variant.name for variant in RESEARCH_VARIANTS} | set(registry.variants)
    assert not any("_Global" in name for name in active_names)
    meta = registry.get_meta("CalmMomentumPut_Continuation")
    assert meta.strategy_type == "TRADE_ELIGIBLE"
    assert meta.can_hard_trade


def test_production_rosters_follow_strategy_family_policy():
    registry = get_strategy_family_registry()
    df = pd.DataFrame({
        "regime": ["stress", "calm", "stress"],
        "rsi14": [50.0] * 3,
        "rsi5": [50.0] * 3,
        "vix_close": [12.0] * 3,
        "vix_chg_1d": [0.0] * 3,
        "vix_chg_pct": [0.0] * 3,
        "ma20_slope": [0.0] * 3,
        "ma10d_slope": [0.0] * 3,
        "ma5d_slope": [0.0] * 3,
        "ma10": [100.0] * 3,
        "ma20": [100.0] * 3,
        "volume_day": [100000.0] * 3,
        "volume_20d": [100000.0] * 3,
        "bb_width": [0.05] * 3,
        "bb_upper": [110.0] * 3,
        "bb_lower": [90.0] * 3,
        "close_1515": [100.0] * 3,
        "high_day": [101.0] * 3,
        "low_day": [99.0] * 3,
        "atr14": [1.0] * 3,
        "volatility_10d": [0.004] * 3,
        "ret_3d": [0.0] * 3,
        "ret_5d": [0.0] * 3,
        "ret_10d": [0.0] * 3,
        "range_position_10d": [0.5] * 3,
        "range_position_20d": [0.5] * 3,
        "support_distance_10d": [0.02] * 3,
        "resistance_distance_10d": [0.02] * 3,
        "room_to_validated_resistance_10d": [0.02] * 3,
        "trend_efficiency_10d": [0.2] * 3,
        "recent_high_20d": [101.0] * 3,
        "global_us_return_mean": [0.0] * 3,
        "global_europe_return_mean": [0.0] * 3,
        "global_asia_return_mean": [0.0] * 3,
        "near_validated_support_10d": [False] * 3,
        "support_broken_10d": [False] * 3,
        "resistance_broken_10d": [False] * 3,
    })

    hard_names = {
        name
        for regime_signals in gather_regime_signals(df, PROMOTED_REGIME_FAMILIES).values()
        for name in regime_signals
    }
    watch_names = {
        name
        for regime_signals in gather_regime_signals(df, WATCH_ONLY_REGIME_FAMILIES).values()
        for name in regime_signals
    }

    assert hard_names
    assert watch_names
    assert hard_names.isdisjoint(watch_names)
    assert {registry.get_meta(name).strategy_type for name in hard_names} == {"TRADE_ELIGIBLE"}
    assert {registry.get_meta(name).strategy_type for name in watch_names} == {"WATCH_ONLY"}


def test_trade_eligible_votes_without_precision_floor_or_min_fire_gate():
    df = pd.DataFrame({
        "regime": ["stress"],
        "actual_trade_label": [FLAT],
    })
    signals = {"stress": {"DownMomentumPut_HighPrecision": pd.Series([PUT])}}

    final, eligibility = build_regime_cascade(df, signals, {"stress": df})

    assert final.tolist() == [PUT]
    assert "DownMomentumPut_HighPrecision" in eligibility["stress"][1]


def test_watch_only_does_not_hard_trade_even_without_precision_gate():
    df = pd.DataFrame({
        "regime": ["stress"],
        "actual_trade_label": [CALL],
    })
    signals = {"stress": {"OversoldBounceCall_MoreTrades": pd.Series([CALL])}}

    final, eligibility = build_regime_cascade(df, signals, {"stress": df})

    assert final.tolist() == [FLAT]
    assert "OversoldBounceCall_MoreTrades" not in eligibility["stress"][0]


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


def test_bollinger_mean_reversion_uses_support_resistance_break_guards():
    df = pd.DataFrame({
        "close_1515": [90.0, 90.0, 110.0, 110.0],
        "bb_lower": [95.0, 95.0, 95.0, 95.0],
        "bb_upper": [105.0, 105.0, 105.0, 105.0],
        "vix_close": [11.9, 12.0, 12.0, 12.0],
        "rsi14": [50.0, 50.0, 50.0, 50.0],
        "support_broken_10d": [False, False, False, False],
        "resistance_broken_10d": [False, False, False, True],
    })
    signal = mean_reversion(df)["strategy_BollingerMeanReversion_signal"]
    assert signal.tolist() == [FLAT, CALL, PUT, FLAT]


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


def test_oversold_bounce_more_trades_uses_validated_room_fallback_and_breakdown_guard():
    df = pd.DataFrame({
        "regime": ["stress"] * 4,
        "vix_close": [12.0] * 4,
        "rsi14": [42.0] * 4,
        "room_to_validated_resistance_10d": [0.026, pd.NA, 0.026, 0.020],
        "resistance_distance_10d": [0.010, 0.026, 0.030, 0.030],
        "support_broken_10d": [False, False, True, False],
        "near_validated_support_10d": [False] * 4,
        "ma20_slope": [0.0] * 4,
        "ma10d_slope": [0.0] * 4,
        "trend_efficiency_10d": [0.0] * 4,
        "ret_5d": [0.0] * 4,
        "volume_hybrid": [1.0] * 4,
        "atr14": [100.0] * 4,
        "close_1515": [10000.0] * 4,
    })
    signal = oversold_bounce_call(df)["strategy_OversoldBounceCall_MoreTrades_signal"]
    assert signal.tolist() == [CALL, CALL, FLAT, FLAT]


def test_calm_trend_applies_break_guards_and_calm_fade_is_removed():
    trend_df = pd.DataFrame({
        "bb_width": [0.040, 0.040],
        "ma20_slope": [0.001, 0.001],
        "room_to_validated_resistance_10d": [0.016, 0.016],
        "resistance_distance_10d": [0.001, 0.001],
        "ma10d_slope": [0.0, 0.0],
        "rsi14": [50.0, 50.0],
        "range_position_10d": [0.4, 0.4],
        "trend_efficiency_10d": [0.25, 0.25],
        "support_broken_10d": [False, True],
        "atr14": [100.0, 100.0],
        "close_1515": [10000.0, 10000.0],
    })
    trend = calm_trend_call(trend_df)
    assert trend["strategy_CalmTrendCall_Headroom_signal"].tolist() == [CALL, FLAT]

    fade_df = pd.DataFrame({
        "bb_width": [0.040] * 40,
        "rsi14": [60.0] * 38 + [80.0, 80.0],
        "rsi5": [70.0] * 38 + [90.0, 90.0],
        "range_position_10d": [0.8] * 40,
        "resistance_broken_10d": [False] * 39 + [True],
    })
    fade = calm_fade_put(fade_df)
    assert fade == {}


def test_research_grid_attributes_promoted_prediction_to_watch_origin():
    df = pd.DataFrame({
        "signal_date": ["2026-07-01", "2026-07-02"],
        "regime": ["stress", "stress"],
        "close_1515": [100.0, 101.0],
        "actual_trade_label": [FLAT, CALL],
    })
    signal = pd.Series([CALL, FLAT])

    promoted, rows = watch_promotion_attribution(df, signal, "OversoldBounceCall_MoreTrades")

    assert promoted.tolist() == [FLAT, CALL]
    assert rows.loc[0, "watch_strategy"] == "OversoldBounceCall_MoreTrades"
    assert rows.loc[0, "watch_signal_date"] == "2026-07-01"
    assert rows.loc[0, "promotion_signal_date"] == "2026-07-02"
