"""Promoted strategy catalog for the cascade.

These are the strategies that have been accepted into the production final
prediction. Each function takes the base feature frame and returns
{signal_column_name: Series of CALL/PUT/NO_POSITION}. Both pipelines import these;
the experiment additionally registers still-experimental strategies of its own.

When an experimental strategy is promoted, move its function + definition here and
add its family to PROMOTED_*_FAMILIES.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.technical_analysis.strategy_families import get_strategy_family_registry

from .constants import CALL, PUT, FLAT, REGIME_STRESS, REGIME_CALM


def _sig(mask: pd.Series, side: str) -> pd.Series:
    return pd.Series(np.where(mask.fillna(False), side, FLAT), index=mask.index)


GLOBAL_REGION_COLS = [
    "global_us_return_mean",
    "global_europe_return_mean",
    "global_asia_overnight_return_mean",
]
GLOBAL_WEIGHTED_TILT_THRESHOLD = 0.001
CONTEXT_ROLLING_WINDOW = 60
CONTEXT_MIN_PERIODS = 30


def _rolling_quantile(series: pd.Series, q: float) -> pd.Series:
    return series.rolling(CONTEXT_ROLLING_WINDOW, min_periods=CONTEXT_MIN_PERIODS).quantile(q).shift(1)


def _rolling_median(series: pd.Series) -> pd.Series:
    return series.rolling(CONTEXT_ROLLING_WINDOW, min_periods=CONTEXT_MIN_PERIODS).median().shift(1)


def _atr_pct(df: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(df["atr14"], errors="coerce") / pd.to_numeric(df["close_1515"], errors="coerce")


def _two_sided_signal(call: pd.Series, put: pd.Series) -> pd.Series:
    sig = np.where(call.fillna(False), CALL, np.where(put.fillna(False), PUT, FLAT))
    return pd.Series(sig, index=call.index)


def _flag(df: pd.DataFrame, column: str, default: bool = False) -> pd.Series:
    if column not in df:
        return pd.Series(default, index=df.index)
    return pd.Series(df[column], index=df.index).fillna(default).astype(bool)


def _upside_room(df: pd.DataFrame) -> pd.Series:
    validated = pd.to_numeric(df.get("room_to_validated_resistance_10d"), errors="coerce")
    raw = pd.to_numeric(df["resistance_distance_10d"], errors="coerce")
    return validated.combine_first(raw)


def _trend_call_context(df: pd.DataFrame) -> pd.Series:
    return (df["ma20_slope"] > 0) & (df["ma10d_slope"] > 0) & (df["trend_efficiency_10d"] >= 0.30)


def _dynamic_call_rsi_cap(df: pd.DataFrame) -> pd.Series:
    rolling_cap = _rolling_quantile(df["rsi14"], 0.70).clip(lower=50.0, upper=62.0)
    trend_cap = pd.Series(np.where(_trend_call_context(df), 58.0, 50.0), index=df.index)
    return pd.concat([rolling_cap, trend_cap], axis=1).max(axis=1).fillna(trend_cap)


def _regional_components(df: pd.DataFrame) -> dict[str, pd.Series]:
    regional = df.reindex(columns=GLOBAL_REGION_COLS).apply(pd.to_numeric, errors="coerce")
    positive_votes = (regional > 0).sum(axis=1)
    negative_votes = (regional < 0).sum(axis=1)
    weighted_mean = regional.mean(axis=1)
    asia = pd.to_numeric(df.get("global_asia_overnight_return_mean", pd.Series(dtype=float)), errors="coerce")
    return {
        # legacy — kept for any unreferenced test code
        "call_agree": positive_votes >= 2,
        "put_agree": negative_votes >= 2,
        "any_call_tailwind": positive_votes >= 1,
        "any_put_tailwind": negative_votes >= 1,
        "weighted_call_tilt": weighted_mean >= GLOBAL_WEIGHTED_TILT_THRESHOLD,
        "weighted_put_tilt": weighted_mean <= -GLOBAL_WEIGHTED_TILT_THRESHOLD,
        # active variants
        "all_neg": negative_votes >= 3,   # all 3 regions negative — suppress CALL
        "all_pos": positive_votes >= 3,   # all 3 regions positive — suppress PUT
        "asia_neg": asia < 0,              # Asia negative — suppress CALL
        "asia_pos": asia > 0,              # Asia positive — suppress PUT
    }


def _with_selected_global_variants(
    df: pd.DataFrame,
    signals: dict[str, pd.Series],
    selected_names: set[str],
) -> dict[str, pd.Series]:
    regional = _regional_components(df)
    out = {col: sig for col, sig in signals.items() if col.replace("strategy_", "").replace("_signal", "") in selected_names}
    for col, sig in signals.items():
        base_name = col.removesuffix("_signal")
        variants = {
            # Suppress CALL only when all 3 global regions are negative
            # (global consensus against the trade); symmetric for PUT.
            f"{base_name}_GlobalAllDisagree_signal": sig.where(
                ~(((sig == CALL) & regional["all_neg"].fillna(False))
                  | ((sig == PUT) & regional["all_pos"].fillna(False))),
                FLAT,
            ),
            # Suppress CALL only when the Asia region is negative;
            # symmetric for PUT. Asia is the most correlated with NIFTY.
            f"{base_name}_GlobalAsiaDisagree_signal": sig.where(
                ~(((sig == CALL) & regional["asia_neg"].fillna(False))
                  | ((sig == PUT) & regional["asia_pos"].fillna(False))),
                FLAT,
            ),
        }
        for variant_col, variant_sig in variants.items():
            name = variant_col.replace("strategy_", "").replace("_signal", "")
            if name in selected_names:
                out[variant_col] = variant_sig
    return out


def _regime_aware_map(df: pd.DataFrame, key: str) -> pd.Series:
    """Build a per-row Series from get_regime_config() for a given threshold key."""
    from src.common.config import get_regime_config
    cfg = get_regime_config()
    regimes = df["regime"] if "regime" in df.columns else pd.Series("stress", index=df.index)
    return regimes.map({
        "stress": cfg["stress"][key],
        "calm":   cfg["calm"][key],
    }).fillna(cfg["stress"][key]).astype(float)


def pullback_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: dips get bought — quiet-tape range lows or intact-uptrend rest."""
    rp20 = pd.to_numeric(
        df["range_position_20d"] if "range_position_20d" in df.columns
        else pd.Series(np.nan, index=df.index), errors="coerce",
    )
    rp10  = pd.to_numeric(df["range_position_10d"], errors="coerce")
    vix   = pd.to_numeric(df["vix_close"], errors="coerce")
    s20   = pd.to_numeric(df["ma20_slope"], errors="coerce")
    s10   = pd.to_numeric(df["ma10d_slope"], errors="coerce")
    room  = _upside_room(df)
    bbw   = pd.to_numeric(df["bb_width"], errors="coerce")
    support_ok = ~_flag(df, "support_broken_10d")
    rsi5  = pd.to_numeric(
        df["rsi5"] if "rsi5" in df.columns else pd.Series(np.nan, index=df.index),
        errors="coerce",
    )
    vix_qmax = _regime_aware_map(df, "vix_quiet_max")
    bb_min   = _regime_aware_map(df, "bb_width_min")
    return {
        "strategy_PullbackCall_QuietVol_signal":
            _sig((rp20 <= 0.25) & (vix <= vix_qmax), CALL),
        "strategy_PullbackCall_DeepWashout_signal":
            _sig((rp20 <= 0.25) & (vix <= vix_qmax) & (rsi5 <= 30), CALL),
        "strategy_PullbackCall_TrendIntact_signal":
            _sig((s20 >= 0.003) & (rp10 <= 0.20) & (room >= 0.015) & support_ok, CALL),
        "strategy_PullbackCall_TrendRest_signal":
            _sig((s20 > 0) & (s10 <= 0) & (room >= 0.015) & (bbw >= bb_min) & support_ok, CALL),
    }


def decline_continuation_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: young ATR-scaled decline extends next session (both regimes)."""
    ret3     = pd.to_numeric(df["ret_3d"], errors="coerce")
    s5       = pd.to_numeric(df["ma5d_slope"], errors="coerce")
    rp10     = pd.to_numeric(df["range_position_10d"], errors="coerce")
    bbw      = pd.to_numeric(df["bb_width"], errors="coerce")
    atr_frac = _atr_pct(df)
    bb_min   = _regime_aware_map(df, "bb_width_min")
    base = (ret3 <= -0.5 * atr_frac) & (s5 < 0) & (rp10 >= 0.20) & (bbw >= bb_min)
    return {"strategy_DeclineContinuationPut_ATR_signal": _sig(base, PUT)}


def _momentum_directional_signals(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Internal: produce MomentumDirectional variants used by expansion_votes."""
    rsi, ret5, room, rp10 = df["rsi14"], df["ret_5d"], df["resistance_distance_10d"], df["range_position_10d"]
    s20, s10, vol, bbw, ret10 = df["ma20_slope"], df["ma10d_slope"], df["volume_day"], df["bb_width"], df["ret_10d"]

    call_votes = ((rsi <= 42).astype(int) + (ret5 < -0.012).astype(int)
                  + (room >= 0.025).astype(int) + (rp10 <= 0.25).astype(int))
    put_votes = (((s20 <= -0.003) | (s10 <= -0.004)).astype(int)
                 + (ret10 <= -0.005).astype(int) + (vol >= 88000).astype(int)
                 + (bbw >= 0.055).astype(int) + (rp10 <= 0.40).astype(int))
    call_strength = call_votes / 4.0
    put_strength = put_votes / 5.0

    trend_context = _trend_call_context(df)
    call_rsi_cap = _dynamic_call_rsi_cap(df)
    context_call_votes = (
        (rsi <= call_rsi_cap).astype(int)
        + (ret5 <= _rolling_quantile(ret5, 0.45).fillna(-0.002)).astype(int)
        + (room >= _rolling_quantile(room, 0.40).fillna(0.006)).astype(int)
        + (rp10 <= np.where(trend_context, 0.90, 0.35)).astype(int)
        + trend_context.astype(int)
    )
    context_put_votes = (
        ((s20 <= _rolling_quantile(s20, 0.35).fillna(-0.003)) | (s10 <= _rolling_quantile(s10, 0.35).fillna(-0.004))).astype(int)
        + (ret10 <= _rolling_quantile(ret10, 0.35).fillna(-0.005)).astype(int)
        + (vol >= np.minimum(88000.0, 1.2 * df["volume_20d"])).astype(int)
        + (bbw >= _rolling_quantile(bbw, 0.55).fillna(0.055)).astype(int)
        + (rp10 <= 0.45).astype(int)
    )
    context_call_fire = context_call_votes >= 3
    context_put_fire = context_put_votes >= 3
    context_pick = np.where((context_put_votes / 5.0) >= (context_call_votes / 5.0), PUT, CALL)
    context_sig = pd.Series(
        np.where(context_call_fire & ~context_put_fire, CALL,
        np.where(context_put_fire & ~context_call_fire, PUT,
        np.where(context_call_fire & context_put_fire, context_pick, FLAT))),
        index=df.index,
    )
    return {
        "strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal": context_sig.where(
            (df["vix_close"] >= 16) & (df["bb_width"] >= 0.065),
            FLAT,
        ),
    }


def _range_breakout_put(df: pd.DataFrame) -> pd.Series:
    """Internal: PUT at/below prior 20-session low with expansion and broken support."""
    close = df["close_1515"].astype(float)
    prior_low = df["low_day"].astype(float).shift(1).rolling(20).min()
    return _sig(
        (close <= prior_low)
        & (df["bb_width"] >= 0.065)
        & _flag(df, "support_broken_10d"),
        PUT,
    )


def expansion_votes(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: two-sided high-vol expansion context vote (vix>=16, bb_width>=6.5%)."""
    raw = _momentum_directional_signals(df)
    strong = raw.get(
        "strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal",
        pd.Series(FLAT, index=df.index),
    )

    # GuardedExpansionVotes_Strong: same fire conditions but PUT suppressed when
    # price is oversold (rsi5 < 30) or within ~1 ATR of validated 10d support.
    rsi5 = pd.to_numeric(
        df["rsi5"] if "rsi5" in df.columns else pd.Series(np.nan, index=df.index),
        errors="coerce",
    )
    close_safe = pd.to_numeric(df["close_1515"], errors="coerce").replace(0, float("nan"))
    atr14 = pd.to_numeric(df["atr14"], errors="coerce")
    sup_dist = pd.to_numeric(df["support_distance_10d"], errors="coerce")
    atr_relative = (atr14 / close_safe).fillna(float("inf"))
    suppress_put = (rsi5 < 30).fillna(False) | (sup_dist < atr_relative).fillna(False)
    guarded = strong.copy()
    guarded[suppress_put & (guarded == PUT)] = FLAT

    return {
        "strategy_ExpansionVotes_Strong_signal": strong,
        "strategy_GuardedExpansionVotes_Strong_signal": guarded,
    }


def breakdown_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: PUT at/below prior 20-session low with expansion + broken support."""
    put_base = _range_breakout_put(df)
    return {"strategy_BreakdownPut_20d_signal": put_base}


def rsi_reversion(df: pd.DataFrame) -> dict[str, pd.Series]:
    """VOTE_ONLY: RSI oversold/overbought mean-reversion."""
    rsi = df["rsi14"]
    sig = np.where(rsi <= 40.0, CALL, np.where(rsi >= 60.0, PUT, FLAT))
    return {"strategy_RsiReversion_6040_signal": pd.Series(sig, index=df.index)}


def trend_down_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """VOTE_ONLY: established downtrend + volume confirmation + VIX floor."""
    s20    = df["ma20_slope"]
    vol    = df["volume_day"]
    vol20  = df["volume_20d"]
    vix    = df["vix_close"]
    vfloor = np.minimum(90000.0, 1.2 * vol20)
    volume_ok = (vol >= vfloor) | vol.isna() | vol20.isna()
    return {
        "strategy_TrendDownPut_Vote_signal": _sig((s20 <= -0.003) & volume_ok & (vix >= 12), PUT),
    }


def fast_drop_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: 5-day velocity drop extends."""
    ret5     = pd.to_numeric(df["ret_5d"], errors="coerce")
    ret2     = pd.to_numeric(df["ret_2d"], errors="coerce")
    atr_frac = _atr_pct(df)
    return {
        "strategy_FastDropPut_5d_signal":           _sig(ret5 <= -0.015, PUT),
        "strategy_FastDropPut_Accelerating_signal": _sig((ret5 <= -0.015) & (ret2 <= -0.010), PUT),
        "strategy_FastDropPut_ATR_signal":          _sig((ret5 <= -1.5 * atr_frac) & (ret2 <= -0.5 * atr_frac), PUT),
    }


def global_shock_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: overnight global weakness carries into NIFTY."""
    asia  = pd.to_numeric(
        df["global_asia_overnight_return_mean"] if "global_asia_overnight_return_mean" in df.columns
        else pd.Series(np.nan, index=df.index), errors="coerce",
    )
    gmean = pd.to_numeric(
        df["global_return_mean"] if "global_return_mean" in df.columns
        else pd.Series(np.nan, index=df.index), errors="coerce",
    )
    rdist = pd.to_numeric(df["resistance_distance_10d"], errors="coerce")
    return {
        "strategy_GlobalShockPut_AsiaRoom_signal":   _sig((asia  <= -0.005) & (rdist >= 0.020), PUT),
        "strategy_GlobalShockPut_AllRegions_signal": _sig((gmean <= -0.005) & (rdist >= 0.020), PUT),
        "strategy_GlobalShockPut_Tail_signal":       _sig((gmean <= -0.010) | (asia <= -0.015), PUT),
    }


def band_reversion(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: mean reversion from Bollinger extremes with break guards."""
    close, upper, lower = df["close_1515"], df["bb_upper"], df["bb_lower"]
    vix_ok     = df["vix_close"] >= 12
    boll_call  = vix_ok & (close < lower) & ~_flag(df, "support_broken_10d")
    boll_put   = vix_ok & (close > upper) & ~_flag(df, "resistance_broken_10d")
    return {
        "strategy_BandReversion_2SD_signal": pd.Series(
            np.where(boll_call, CALL, np.where(boll_put, PUT, FLAT)), index=df.index
        ),
    }


def squeeze_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: compressed BB width + rising VIX resolves downward."""
    bbw  = pd.to_numeric(df["bb_width"], errors="coerce")
    dvix = pd.to_numeric(df["vix_chg_1d"], errors="coerce")
    s5   = pd.to_numeric(df["ma5d_slope"], errors="coerce")
    base = (bbw <= 0.040) & (dvix > 0)
    return {
        "strategy_SqueezePut_MoreTrades_signal":    _sig(base, PUT),
        "strategy_SqueezePut_HighPrecision_signal": _sig(base & (s5 < 0), PUT),
    }


def rally_continuation_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: multiple rally-continuation CALL hypotheses."""
    bb_min  = _regime_aware_map(df, "bb_width_min")
    rsi5    = pd.to_numeric(df["rsi5"] if "rsi5" in df.columns else pd.Series(np.nan, index=df.index), errors="coerce")
    rsi14   = pd.to_numeric(df["rsi14"], errors="coerce")
    dvix    = pd.to_numeric(df["vix_chg_1d"], errors="coerce")
    vcp     = pd.to_numeric(df.get("vix_chg_pct",  pd.Series(np.nan, index=df.index)), errors="coerce")
    s20     = pd.to_numeric(df["ma20_slope"],    errors="coerce")
    s10     = pd.to_numeric(df["ma10d_slope"],   errors="coerce")
    s5      = pd.to_numeric(df["ma5d_slope"],    errors="coerce")
    ret3    = pd.to_numeric(df["ret_3d"],        errors="coerce")
    ret5    = pd.to_numeric(df["ret_5d"],        errors="coerce")
    rp10    = pd.to_numeric(df["range_position_10d"], errors="coerce")
    bbw     = pd.to_numeric(df["bb_width"],      errors="coerce")
    room    = _upside_room(df)
    vix     = pd.to_numeric(df["vix_close"],     errors="coerce")
    vh      = pd.to_numeric(df.get("volume_hybrid",  pd.Series(np.nan, index=df.index)), errors="coerce")
    sc      = pd.to_numeric(df.get("ma_slope_combo", pd.Series(np.nan, index=df.index)), errors="coerce")
    te      = pd.to_numeric(df["trend_efficiency_10d"], errors="coerce")
    return {
        "strategy_RallyContinuationCall_VixDrain_signal":
            _sig((rsi5 >= 70) & (dvix < 0), CALL),
        "strategy_RallyContinuationCall_VixDrainTrend_signal":
            _sig((rsi5 >= 70) & (dvix < 0) & (s20 > 0), CALL),
        "strategy_RallyContinuationCall_VixDrainQuiet_signal":
            _sig((rsi5 >= 70) & (dvix < 0) & (s20 > 0) & (vix <= 13), CALL),
        "strategy_RallyContinuationCall_3dFollowThrough_signal":
            _sig((ret3 >= 0.003) & (s5 > 0) & (s10 >= -0.001) & (rp10 <= 0.95) & (bbw >= bb_min), CALL),
        "strategy_RallyContinuationCall_FullStack_signal":
            _sig(
                (s20 >= 0.003) & (sc > 0) & (vh >= 1.0)
                & (ret3 > 0) & (ret5 > 0) & (rp10 >= 0.60) & (rp10 <= 1.05)
                & (te >= 0.15) & (vix >= 12) & (vcp <= 0.03),
                CALL,
            ),
        "strategy_RallyContinuationCall_Breather_signal":
            _sig((rsi14 >= 60) & (s5 < 0), CALL),
        "strategy_RallyContinuationCall_BreatherRoom_signal":
            _sig((rsi14 >= 60) & (s5 < 0) & (room >= 0.015), CALL),
    }
def recovery_drift_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: recovery drift CALL after a recent sharp 2-day drawdown.

    Fires when a -1.5%+ 2-day shock occurred within the last 5 sessions and
    the tape has since stabilised: rising 20d slope, bouncing price action
    (5d slope positive or close above 5d MA), VIX draining, and mid-range
    position (not yet overbought).
    """
    ret2   = pd.to_numeric(df["ret_2d"],            errors="coerce")
    s20    = pd.to_numeric(df["ma20_slope"],         errors="coerce")
    s5     = pd.to_numeric(df["ma5d_slope"],         errors="coerce")
    dvix   = pd.to_numeric(df["vix_chg_1d"],         errors="coerce")
    rp20   = pd.to_numeric(df["range_position_20d"], errors="coerce")
    close  = pd.to_numeric(df["close_1515"],         errors="coerce")
    ma5    = close.rolling(5, min_periods=5).mean()

    shock_ok  = ret2.rolling(5, min_periods=1).min() <= -0.015  # -1.5% hit in last 5 sessions
    trend_ok  = s20 > 0.003                                      # ma20_slope > +0.3%
    bounce_ok = (s5 > 0) | (close > ma5)                        # 5d slope rising OR price above 5d MA
    vix_ok    = dvix < 0                                         # VIX draining
    rp_ok     = (rp20 >= 0.30) & (rp20 <= 0.85)                 # mid-range, not overbought

    return {
        "strategy_RecoveryDriftCall_signal":
            _sig(shock_ok & trend_ok & bounce_ok & vix_ok & rp_ok, CALL),
    }


_PRODUCTION_STRESS_FAMILIES = {
    # SIGNAL — all-regime
    "PullbackCall":           pullback_call,
    "DeclineContinuationPut": decline_continuation_put,
    # SIGNAL — stress-only
    "ExpansionVotes": expansion_votes,
    "BreakdownPut":   breakdown_put,
    # VOTE_ONLY — all-regime
    "RsiReversion": rsi_reversion,
    "TrendDownPut":  trend_down_put,
}
_PRODUCTION_CALM_FAMILIES = {
    # SIGNAL — all-regime
    "PullbackCall":           pullback_call,
    "DeclineContinuationPut": decline_continuation_put,
    # VOTE_ONLY — all-regime
    "RsiReversion": rsi_reversion,
    "TrendDownPut":  trend_down_put,
}


def _filter_by_strategy_type(fn, allowed_types: set[str]):
    def _filtered(df: pd.DataFrame) -> dict[str, pd.Series]:
        registry = get_strategy_family_registry()
        out: dict[str, pd.Series] = {}
        for col, sig in fn(df).items():
            name = col.replace("strategy_", "").replace("_signal", "")
            try:
                meta = registry.get_meta(name)
            except KeyError:
                continue
            if meta.strategy_type in allowed_types:
                out[col] = sig
        return out

    return _filtered


PROMOTED_STRESS_FAMILIES = {
    name: _filter_by_strategy_type(fn, {"TRADE_ELIGIBLE"})
    for name, fn in _PRODUCTION_STRESS_FAMILIES.items()
}

PROMOTED_CALM_FAMILIES = {
    name: _filter_by_strategy_type(fn, {"TRADE_ELIGIBLE"})
    for name, fn in _PRODUCTION_CALM_FAMILIES.items()
}

PROMOTED_REGIME_FAMILIES = {
    REGIME_STRESS: PROMOTED_STRESS_FAMILIES,
    REGIME_CALM: PROMOTED_CALM_FAMILIES,
}

# All participating families: SIGNAL + VOTE_ONLY (production cascade Steps 1-4).
ALL_PARTICIPATING_STRESS_FAMILIES = {
    name: _filter_by_strategy_type(fn, {"SIGNAL", "VOTE_ONLY", "TRADE_ELIGIBLE", "WATCH_ONLY"})
    for name, fn in _PRODUCTION_STRESS_FAMILIES.items()
}
ALL_PARTICIPATING_CALM_FAMILIES = {
    name: _filter_by_strategy_type(fn, {"SIGNAL", "VOTE_ONLY", "TRADE_ELIGIBLE", "WATCH_ONLY"})
    for name, fn in _PRODUCTION_CALM_FAMILIES.items()
}
ALL_PARTICIPATING_REGIME_FAMILIES = {
    REGIME_STRESS: ALL_PARTICIPATING_STRESS_FAMILIES,
    REGIME_CALM: ALL_PARTICIPATING_CALM_FAMILIES,
}
# Backward-compatibility alias (was the watch-only sub-roster; now unified).
WATCH_ONLY_REGIME_FAMILIES = ALL_PARTICIPATING_REGIME_FAMILIES


# Human-readable definitions for the promoted strategies, keyed by metric name
# (signal column without the strategy_ prefix and _signal suffix).
PROMOTED_DEFINITIONS: dict[str, str] = {}
