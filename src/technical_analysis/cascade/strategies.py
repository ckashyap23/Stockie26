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
    "global_asia_return_mean",
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


def _severe_efficient_downside_breakdown(df: pd.DataFrame) -> pd.Series:
    return (
        (pd.to_numeric(df["ma20_slope"], errors="coerce") <= -0.003)
        & (pd.to_numeric(df["trend_efficiency_10d"], errors="coerce") >= 0.30)
        & (pd.to_numeric(df["ret_5d"], errors="coerce") < 0)
        & (pd.to_numeric(df.get("volume_hybrid"), errors="coerce") >= 1.10)
    )


def _trend_call_context(df: pd.DataFrame) -> pd.Series:
    return (df["ma20_slope"] > 0) & (df["ma10d_slope"] > 0) & (df["trend_efficiency_10d"] >= 0.30)


def _dynamic_call_rsi_cap(df: pd.DataFrame) -> pd.Series:
    rolling_cap = _rolling_quantile(df["rsi14"], 0.70).clip(lower=50.0, upper=62.0)
    trend_cap = pd.Series(np.where(_trend_call_context(df), 58.0, 50.0), index=df.index)
    return pd.concat([rolling_cap, trend_cap], axis=1).max(axis=1).fillna(trend_cap)


def _dynamic_room_floor(df: pd.DataFrame) -> pd.Series:
    rolling_floor = _rolling_quantile(df["resistance_distance_10d"], 0.40).clip(lower=0.004, upper=0.025)
    atr_floor = (0.25 * _atr_pct(df)).clip(lower=0.004, upper=0.020)
    return pd.concat([rolling_floor, atr_floor], axis=1).min(axis=1).fillna(0.006)


def _regional_components(df: pd.DataFrame) -> dict[str, pd.Series]:
    regional = df.reindex(columns=GLOBAL_REGION_COLS).apply(pd.to_numeric, errors="coerce")
    positive_votes = (regional > 0).sum(axis=1)
    negative_votes = (regional < 0).sum(axis=1)
    weighted_mean = regional.mean(axis=1)
    asia = pd.to_numeric(df.get("global_asia_return_mean", pd.Series(dtype=float)), errors="coerce")
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


def _apply_global_disagree_both(sig: pd.Series, df: pd.DataFrame) -> pd.Series:
    """Apply GlobalAllDisagree AND GlobalAsiaDisagree filters to a signal.

    Suppresses CALL when all_neg OR asia_neg is True (the combined filter is
    equivalent to GlobalAsiaDisagree since all_neg ⊆ asia_neg), and PUT when
    all_pos OR asia_pos is True. Written explicitly to document that both
    directions of global context are intentionally applied.

    Used by the promoted-only wrapper functions so the base signal in the
    production cascade always carries global context without needing a separate
    _GlobalXxx suffix variant.
    """
    regional = _regional_components(df)
    suppress = (
        ((sig == CALL) & (regional["all_neg"].fillna(False) | regional["asia_neg"].fillna(False)))
        | ((sig == PUT) & (regional["all_pos"].fillna(False) | regional["asia_pos"].fillna(False)))
    )
    return sig.where(~suppress, FLAT)


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


def oversold_bounce_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    rsi, room = df["rsi14"], _upside_room(df)
    vix = df["vix_close"]
    stress = df["regime"].eq(REGIME_STRESS) if "regime" in df else pd.Series(True, index=df.index)
    support_ok = ~_flag(df, "support_broken_10d")
    no_severe_breakdown = ~_severe_efficient_downside_breakdown(df)
    call_rsi_cap = _dynamic_call_rsi_cap(df)
    room_floor = _dynamic_room_floor(df)
    signals = {
        "strategy_OversoldBounceCall_MoreTrades_signal":
            _sig(stress & (rsi <= 42) & (room >= 0.025) & (vix >= 12) & support_ok & no_severe_breakdown, CALL),
        "strategy_OversoldBounceCall_ContextRoom_signal":
            _sig(stress & (rsi <= call_rsi_cap) & (room >= room_floor) & (vix >= 12) & support_ok, CALL),
    }
    return _with_selected_global_variants(df, signals, {
        "OversoldBounceCall_MoreTrades",
        "OversoldBounceCall_ContextRoom",
    })


def down_momentum_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    s20, vol, dvix, vix = df["ma20_slope"], df["volume_day"], df["vix_chg_1d"], df["vix_close"]
    vol20 = df["volume_20d"]
    # Hybrid volume floor: the absolute conviction level, but allowed to relax
    # proportionally when the trailing 20-day average volume itself is depressed
    # (e.g. the light post-expiry week). min() keeps the in-sample fire set
    # identical to the old fixed floor (vol20 averages ~96k here, so 1.2*vol20
    # exceeds the absolute floor on all but the genuinely thin-volume days),
    # while adapting downward in a structurally lighter-volume regime.
    vfloor = np.minimum(90000.0, 1.2 * vol20)
    # Missing volume inputs are neutral for this AND gate: absence of the
    # optional confirmation must not suppress an otherwise valid setup.
    volume_ok = (vol >= vfloor) | vol.isna() | vol20.isna()
    signals = {
        "strategy_DownMomentumPut_HighPrecision_signal":
            _sig((s20 <= -0.003) & volume_ok & (dvix > 0), PUT),
        "strategy_DownMomentumPut_MoreTrades_signal":
            _sig((s20 <= -0.003) & volume_ok & (vix >= 12), PUT),
    }
    return _with_selected_global_variants(df, signals, {
        "DownMomentumPut_HighPrecision",
        "DownMomentumPut_MoreTrades",
    })


def momentum_directional(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Merged best-balanced CALL + PUT into one two-sided directional signal.

    CALL fires on >=2 oversold-reversion votes (max 4); PUT fires on >=3
    down-momentum votes (max 5). When both sides fire on the same day the
    conflict is resolved by normalised vote strength (votes / max_votes): the
    side that is more strongly confirmed wins. This vote-margin tie-break is
    far better than dropping conflicts, because oversold and down-momentum
    conditions overlap heavily on falling days.
    """
    rsi, ret5, room, rp10 = df["rsi14"], df["ret_5d"], df["resistance_distance_10d"], df["range_position_10d"]
    s20, s10, vol, bbw, ret10 = df["ma20_slope"], df["ma10d_slope"], df["volume_day"], df["bb_width"], df["ret_10d"]

    call_votes = ((rsi <= 42).astype(int) + (ret5 < -0.012).astype(int)
                  + (room >= 0.025).astype(int) + (rp10 <= 0.25).astype(int))
    put_votes = (((s20 <= -0.003) | (s10 <= -0.004)).astype(int)
                 + (ret10 <= -0.005).astype(int) + (vol >= 88000).astype(int)
                 + (bbw >= 0.055).astype(int) + (rp10 <= 0.40).astype(int))

    call_fire = call_votes >= 2
    put_fire = put_votes >= 3
    call_strength = call_votes / 4.0
    put_strength = put_votes / 5.0
    conflict_pick = np.where(put_strength >= call_strength, PUT, CALL)
    sig = np.where(call_fire & ~put_fire, CALL,
          np.where(put_fire & ~call_fire, PUT,
          np.where(call_fire & put_fire, conflict_pick, FLAT)))
    signals = {"strategy_MomentumDirectional_signal": pd.Series(sig, index=df.index)}

    trend_context = _trend_call_context(df)
    call_rsi_cap = _dynamic_call_rsi_cap(df)
    room_floor = _dynamic_room_floor(df)
    context_call_votes = (
        (rsi <= call_rsi_cap).astype(int)
        + (ret5 <= _rolling_quantile(ret5, 0.45).fillna(-0.002)).astype(int)
        + (room >= room_floor).astype(int)
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
    signals["strategy_MomentumDirectional_ContextVotes_ExpansionGuard_signal"] = context_sig.where(
        (df["bb_width"] >= 0.055) & (df["resistance_distance_10d"] >= 0.015),
        FLAT,
    )
    signals["strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal"] = context_sig.where(
        (df["vix_close"] >= 16) & (df["bb_width"] >= 0.065),
        FLAT,
    )
    return _with_selected_global_variants(df, signals, {
        "MomentumDirectional",
        "MomentumDirectional_ContextVotes_ExpansionGuard",
        "MomentumDirectional_ContextVotes_StrongExpansionGuard",
    })


def mean_reversion(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Merged mean-reversion family with support/resistance break guards."""
    close, upper, lower = df["close_1515"], df["bb_upper"], df["bb_lower"]
    vix_ok = df["vix_close"] >= 12
    boll_call = vix_ok & (close < lower) & ~_flag(df, "support_broken_10d")
    boll_put = vix_ok & (close > upper) & ~_flag(df, "resistance_broken_10d")
    boll = np.where(boll_call, CALL, np.where(boll_put, PUT, FLAT))

    rsi = df["rsi14"]
    rsi_mr = np.where(rsi <= 40.0, CALL, np.where(rsi >= 60.0, PUT, FLAT))

    signals = {
        "strategy_BollingerMeanReversion_signal": pd.Series(boll, index=df.index),
        "strategy_RsiMeanReversion_6040_signal": pd.Series(rsi_mr, index=df.index),
    }
    return _with_selected_global_variants(df, signals, {
        "BollingerMeanReversion",
        "RsiMeanReversion_6040",
    })


def macd_ema(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = pd.to_numeric(df["close_1515"], errors="coerce")
    fast = close.ewm(span=5, adjust=False, min_periods=5).mean()
    slow = close.ewm(span=20, adjust=False, min_periods=20).mean()
    macd = fast - slow
    call = (macd > 0) & (macd.shift(1) <= 0)
    put = (macd < 0) & (macd.shift(1) >= 0)
    return {"strategy_MACD_EMA5_20_signal": _two_sided_signal(call, put)}


def _ma_alignment_room_base(df: pd.DataFrame) -> pd.Series:
    close = df["close_1515"].astype(float)
    ma5 = close.rolling(5).mean()
    ma10, ma20 = df["ma10"], df["ma20"]
    rsi = df["rsi14"]
    rdist, sdist = df["resistance_distance_10d"], df["support_distance_10d"]
    spread = (ma10 - ma20) / ma20
    call = (ma5 > ma10) & (spread > 0.0) & (rsi < 50.0) & (rdist > 0.005)
    put = (ma5 < ma10) & (spread < -0.0005) & (rsi > 30.0) & (sdist > 0.0)
    return _two_sided_signal(call, put)


def ma_alignment_room(df: pd.DataFrame) -> dict[str, pd.Series]:
    base = _ma_alignment_room_base(df)
    ret5, rp10, sdist = df["ret_5d"], df["range_position_10d"], df["support_distance_10d"]
    guard_ok = (ret5 < 0) & (rp10 < 0.5) & (sdist <= 0.02)
    put_guarded = base.where(~((base == PUT) & ~guard_ok.fillna(False)), FLAT)

    rsi, ret10, rdist = df["rsi14"], df["ret_10d"], df["resistance_distance_10d"]
    rebound = (rsi.between(25, 45)) & (rdist > 0.02) & (sdist >= 0) & (ret10 < 0) & (ret5 > ret10)
    spread = (df["ma10"] - df["ma20"]) / df["ma20"]
    signals = {
        "strategy_MAAlignmentRoom_PutGuarded_signal": put_guarded,
        "strategy_MAAlignmentRoom_ReboundCall_signal": _sig(rebound, CALL),
        "strategy_MaTrend_001_signal": pd.Series(
            np.where(spread > 0.001, CALL, np.where(spread < -0.001, PUT, FLAT)),
            index=df.index,
        ),
    }
    return _with_selected_global_variants(df, signals, {
        "MAAlignmentRoom_ReboundCall",
        "MAAlignmentRoom_PutGuarded",
        "MaTrend_001",
    })


def range_breakout(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close_1515"].astype(float)
    prior_high = df["high_day"].astype(float).shift(1).rolling(20).max()
    prior_low = df["low_day"].astype(float).shift(1).rolling(20).min()
    atr_buffer = 0.20 * df["atr14"].astype(float)
    signals = {
        "strategy_RangeBreakout_signal": pd.Series(
            np.where(close > prior_high, CALL, np.where(close < prior_low, PUT, FLAT)),
            index=df.index,
        ),
        "strategy_RangeBreakout_ATRBuffer_signal": pd.Series(
            np.where(close > (prior_high - atr_buffer), CALL, np.where(close < (prior_low + atr_buffer), PUT, FLAT)),
            index=df.index,
        ),
    }
    out = _with_selected_global_variants(df, signals, {
        "RangeBreakout",
        "RangeBreakout_ATRBuffer",
    })
    out["strategy_RangeBreakoutPut_signal"] = _sig(
        (close <= prior_low)
        & (df["bb_width"] >= 0.065)
        & _flag(df, "support_broken_10d"),
        PUT,
    )
    return out


def stress_watch_candidates(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Stress-regime watch-only candidates; never part of the hard cascade."""
    stress = df["regime"].eq(REGIME_STRESS) if "regime" in df else pd.Series(True, index=df.index)
    volume_hybrid = pd.to_numeric(df.get("volume_hybrid"), errors="coerce")
    slope_combo = pd.to_numeric(df.get("ma_slope_combo"), errors="coerce")
    up_momentum_call = (
        stress
        & (df["ma20_slope"] >= 0.003)
        & (slope_combo > 0)
        & (volume_hybrid >= 1.0)
        & (df["ret_3d"] > 0)
        & (df["ret_5d"] > 0)
        & df["range_position_10d"].between(0.60, 1.05)
        & (df["trend_efficiency_10d"] >= 0.15)
        & (df["vix_close"] >= 12)
        & (df["vix_chg_pct"] <= 0.03)
    )
    return {
        "strategy_UpMomentumCall_HighPrecision_signal": _sig(up_momentum_call, CALL),
    }


# ───────────────────────── calm-regime strategies ─────────────────────────
# The calm low-volatility tape rarely prints a 0.5% intraday move, so these are
# graded at the calm threshold (0.3%). The edge here is trend-continuation in a
# quiet uptrend (buy shallow pullbacks / headroom), plus a small overbought-fade
# PUT — the opposite character to the stressed-tape oversold-bounce / down-momentum
# rules.

def calm_trend_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    s20, rsi, rp = df["ma20_slope"], df["rsi14"], df["range_position_10d"]
    room = _upside_room(df)
    ma10, te = df["ma10d_slope"], df["trend_efficiency_10d"]
    calm_width = df["bb_width"] >= 0.040
    support_ok = ~_flag(df, "support_broken_10d")
    signals = {
        # Headroom dip-buy: quiet 20d uptrend with room to resistance, but only
        # while the 10d slope has rolled over (ma10d_slope <= 0) — i.e. buy the
        # shallow dip inside the uptrend, not an already-extended push. The
        # ma10d filter lifts precision 0.61 -> 0.70 (correct fires pull back
        # first; wrong fires were flat/extended).
        "strategy_CalmTrendCall_Headroom_signal":
            _sig(calm_width & (s20 > 0) & (room >= 0.015) & (ma10 <= 0) & support_ok, CALL),
    }
    room_floor = _dynamic_room_floor(df)
    signals["strategy_CalmTrendCall_ContextHeadroom_signal"] = _sig(
        calm_width
        & (s20 > 0)
        & (room >= room_floor)
        & (rsi <= _dynamic_call_rsi_cap(df))
        & (ma10 <= _rolling_quantile(ma10, 0.60).fillna(0.002))
        & support_ok,
        CALL,
    )
    return {col: sig for col, sig in signals.items() if col.replace("strategy_", "").replace("_signal", "") in {
        "CalmTrendCall_Headroom",
        "CalmTrendCall_ContextHeadroom",
    }}


def calm_fade_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {}


def calm_momentum_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    ret3 = df["ret_3d"]
    calm_width = df["bb_width"] >= 0.040
    signals = {
        # Momentum continuation PUT: the overbought-fade only catches the
        # *reversal* type of calm PUT move (rsi >= 65). The other ~90% of calm
        # PUT-move days are continuation moves from neutral/mildly-weak tapes
        # (median rsi5 ~46, range_position ~0.40) that the fade structurally
        # cannot see. A 3-day decline of >= 0.3% in a calm tape tends to extend
        # >= 0.3% the next day: precision 0.625 vs 0.536 base, recall 0.467 (vs
        # the fade's 0.08) — zero overlap with the fade, so it fills the recall
        # gap without touching the fade's precision.
        "strategy_CalmMomentumPut_Continuation_signal":
            _sig(
                calm_width
                & (ret3 <= -0.003)
                & (df["ma5d_slope"] < 0)
                & (df["range_position_10d"] >= 0.20),
                PUT,
            ),
    }
    return _with_selected_global_variants(df, signals, {
        "CalmMomentumPut_Continuation",
    })


def calm_momentum_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Calm upside-continuation watch candidates."""
    calm = df["regime"].eq(REGIME_CALM) if "regime" in df else pd.Series(True, index=df.index)
    base = (
        calm
        & (df["bb_width"] >= 0.040)
        & (df["ret_3d"] >= 0.003)
        & (df["ma5d_slope"] > 0)
        & (df["ma10d_slope"] >= -0.001)
        & (df["range_position_10d"] <= 0.95)
    )
    return {
        "strategy_CalmMomentumCall_Continuation_signal": _sig(base, CALL),
    }


# ── Production-only promoted wrappers ───────────────────────────────────────
# These wrap the research family functions and define EXACTLY which variants
# participate in the production precision cascade:
#   • Group-A variants (precision never cleared the floor) are excluded.
#   • Strategy-level global index guard variants are excluded. If global context
#     is used again, it should be applied as one final prediction-level layer.
# The underlying research functions are UNCHANGED so the research grid still
# sees every variant.


def _promoted_oversold_bounce_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Legacy wrapper kept for callers that still import it."""
    raw = oversold_bounce_call(df)
    keep = {
        "strategy_OversoldBounceCall_MoreTrades_signal",
    }
    return {k: v for k, v in raw.items() if k in keep}


def _promoted_down_momentum_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Legacy wrapper kept for callers that still import it."""
    raw = down_momentum_put(df)
    keep = {
        "strategy_DownMomentumPut_HighPrecision_signal",
        "strategy_DownMomentumPut_MoreTrades_signal",
    }
    return {k: v for k, v in raw.items() if k in keep}


def _promoted_momentum_directional(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Promoted MomentumDirectional variants: ContextVotes family only.

    Dropped from promoted:
      • MomentumDirectional base (Group B: eligible CALL 0.800 but always
        outcompeted by ContextVotes_ExpansionGuard; retired in favour of the
        more selective context-vote variants)
    """
    raw = momentum_directional(df)
    keep = {
        "strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal",
    }
    return {k: v for k, v in raw.items() if k in keep}


def _promoted_mean_reversion(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Legacy wrapper kept for callers that still import it."""
    raw = mean_reversion(df)
    keep = {
        "strategy_BollingerMeanReversion_signal",
        "strategy_RsiMeanReversion_6040_signal",
    }
    return {k: v for k, v in raw.items() if k in keep}


def _promoted_range_breakout(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Legacy range-breakout wrapper kept for callers that still import it."""
    raw = range_breakout(df)
    key = "strategy_RangeBreakoutPut_signal"
    if key not in raw:
        return {}
    return {key: raw[key].where(raw[key] == PUT, FLAT)}


def _promoted_calm_trend_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Promoted calm CALL variants: Headroom + Pullback.

    Dropped from promoted:
      • CalmTrendCall_ContextHeadroom (Group B: eligible CALL 0.568 in calm but
        always outcompeted by CalmTrendCall_Headroom; never drove a production decision)

    """
    raw = calm_trend_call(df)
    keep = {
        "strategy_CalmTrendCall_Headroom_signal",
    }
    return {k: v for k, v in raw.items() if k in keep}


def _promoted_calm_fade_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Legacy wrapper kept for callers that still import it."""
    raw = calm_fade_put(df)
    keep: set[str] = set()
    return {k: v for k, v in raw.items() if k in keep}


def _promoted_calm_momentum_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Legacy wrapper kept for callers that still import it."""
    raw = calm_momentum_put(df)
    keep = {
        "strategy_CalmMomentumPut_Continuation_signal",
    }
    return {k: v for k, v in raw.items() if k in keep}


# Production families grouped by volatility regime. The raw family functions
# remain shared with research; production participation is controlled solely by
# strategy_families.yaml.
_PRODUCTION_STRESS_FAMILIES = {
    "OversoldBounceCall": oversold_bounce_call,
    "DownMomentumPut": down_momentum_put,
    "MomentumDirectional": momentum_directional,
    "MeanReversion": mean_reversion,
    "MACD_EMA": macd_ema,
    "RangeBreakout": range_breakout,
    "StressWatchCandidates": stress_watch_candidates,
}
_PRODUCTION_CALM_FAMILIES = {
    "MeanReversion": mean_reversion,
    "MACD_EMA": macd_ema,
    "CalmTrendCall": calm_trend_call,
    "CalmMomentumPut": calm_momentum_put,
    "CalmMomentumCall": calm_momentum_call,
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

# Separate from PROMOTED_REGIME_FAMILIES by design: these signals may seed
# watches, but can never become direct hard-cascade predictions.
WATCH_ONLY_REGIME_FAMILIES = {
    REGIME_STRESS: {
        name: _filter_by_strategy_type(fn, {"WATCH_ONLY"})
        for name, fn in _PRODUCTION_STRESS_FAMILIES.items()
    },
    REGIME_CALM: {
        name: _filter_by_strategy_type(fn, {"WATCH_ONLY"})
        for name, fn in _PRODUCTION_CALM_FAMILIES.items()
    },
}


# Human-readable definitions for the promoted strategies, keyed by metric name
# (signal column without the strategy_ prefix and _signal suffix).
PROMOTED_DEFINITIONS: dict[str, str] = {
    "OversoldBounceCall_MoreTrades":
        "Stress CALL when rsi14 <= 42, vix_close >= 12, upside room >= 2.5%, "
        "support_broken_10d is false, and no severe efficient downside breakdown "
        "is active. Upside room uses room_to_validated_resistance_10d when present, "
        "otherwise resistance_distance_10d.",
    "OversoldBounceCall_ContextRoom":
        "CALL when rsi14 is below a rolling 60-day context cap, resistance_distance_10d "
        "clears a rolling/ATR-aware room floor, and vix_close >= 12.",
    "OversoldBounceCall_Guarded":
        "CALL when rsi14 <= 42 AND resistance_distance_10d >= 2.5% AND vix_close >= 12 "
        "(same oversold core as MoreTrades) AND ma20_slope >= -0.01 (broad trend not "
        "in a strong down-leg) AND ma5d_slope >= -0.02 (short slope not in "
        "capitulation). The regime gate uses base slope features to drop the "
        "steep-falling days where the bounce keeps falling, lifting precision.",
    "DownMomentumPut_HighPrecision":
        "PUT when ma20_slope <= -0.003 (falling 20-day MA) AND volume_day >= "
        "min(90,000, 1.2 * volume_20d) AND vix_chg_1d > 0 (India VIX rising). "
        "Downside momentum continuation confirmed by rising fear. The hybrid volume "
        "floor holds the absolute conviction level in a normal-volume regime but "
        "relaxes proportionally when the trailing 20-day average volume is depressed "
        "(e.g. the light post-expiry week), so it adapts to volume drift.",
    "DownMomentumPut_MoreTrades":
        "PUT when ma20_slope <= -0.003 AND volume_day >= min(90,000, 1.2 * volume_20d) "
        "AND vix_close >= 12. Same momentum core but a VIX level gate (instead of "
        "rising-VIX) to trade more.",
    "MomentumDirectional":
        "Two-sided. CALL on >=2 of {rsi14<=42, ret_5d<-1.2%, "
        "resistance_distance_10d>=2.5%, range_position_10d<=0.25}. PUT on >=3 of "
        "{ma20_slope<=-0.003 or ma10d_slope<=-0.004, ret_10d<=-0.5%, "
        "volume_day>=88k, bb_width>=0.055, range_position_10d<=0.40}. When both "
        "sides fire, the side with higher normalised vote strength wins.",
    "MomentumDirectional_ContextVotes_ExpansionGuard":
        "Two-sided context vote variant kept when bb_width >= 5.5% and "
        "resistance_distance_10d >= 1.5%.",
    "MomentumDirectional_ContextVotes_StrongExpansionGuard":
        "Context vote variant kept when vix_close >= 16 and bb_width >= 6.5%.",
    "MAAlignmentRoom_ReboundCall":
        "CALL-only rebound setup: rsi14 in [25,45], resistance room > 2%, non-negative "
        "support room, negative 10-day return, and 5-day return improving vs 10-day return.",
    "MaTrend_001":
        "Two-sided MA10/MA20 spread with a 0.1% dead band: CALL above +0.1%, PUT below -0.1%.",
    "MAAlignmentRoom_PutGuarded":
        "MA alignment signal with PUTs kept only when ret_5d < 0, range_position_10d < 0.5, "
        "and support_distance_10d <= 2%.",
    "BollingerMeanReversion":
        "VIX must be >= 12. CALL below the lower Bollinger band when support_broken_10d "
        "is false; PUT above the upper Bollinger band when resistance_broken_10d is false.",
    "RsiMeanReversion_6040":
        "CALL when rsi14 <= 40; PUT when rsi14 >= 60; else NO_POSITION.",
    "MACD_EMA5_20":
        "CALL when EMA5 minus EMA20 crosses above zero; PUT when it crosses below zero.",
    "RangeBreakout":
        "Two-sided 20-day breakout: CALL above the prior 20-day high, PUT below the prior 20-day low.",
    "RangeBreakout_ATRBuffer":
        "Two-sided 20-day breakout with a 0.20*ATR buffer around the prior high/low.",
    "UpMomentumCall_HighPrecision":
        "[watch-only; hard promotion disabled] Stress CALL continuation with positive "
        "slopes/returns, volume confirmation, trend efficiency, and controlled VIX change.",
    "RangeBreakoutPut":
        "[watch-only] Stress PUT at or below the prior 20-session low with "
        "bb_width >= 6.5% and support_broken_10d true.",
    "CalmTrendCall_Headroom":
        "[calm regime, graded at 0.3%] CALL when ma20_slope > 0 (quiet uptrend) AND "
        "resistance_distance_10d >= 1.5% (headroom to resistance) AND ma10d_slope <= 0 "
        "(the 10-day slope has rolled over — buy the shallow dip, not an extended "
        "push). The dip filter lifts precision ~0.61 -> 0.70.",
    "CalmTrendCall_ContextHeadroom":
        "[calm regime, graded at 0.3%] CALL when quiet uptrend persists, dynamic resistance "
        "room is cleared, rsi14 is below its context cap, and ma10d_slope is not extended.",
    "CalmMomentumPut_Continuation":
        "[calm regime, graded at 0.3%] PUT when ret_3d <= -0.3% — a 3-day decline that "
        "tends to extend >= 0.3% the next day. This is the continuation counterpart to "
        "the overbought fade: it catches the neutral/mildly-weak calm PUT moves (median "
        "rsi5 ~46) the fade cannot see, lifting calm PUT recall 0.08 -> 0.47 at precision "
        "0.625 (base 0.536), with zero overlap with the fade.",
}
