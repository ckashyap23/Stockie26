"""Promoted strategy catalog for the cascade.

These are the strategies that have been accepted into the production final
prediction. Each function takes the base feature frame and returns
{signal_column_name: Series of CALL/PUT/NO_POSITION}. Both pipelines import these;
the experiment additionally registers still-experimental strategies of its own.

When an experimental strategy is promoted, move its function + definition here and
mark it as SIGNAL in the strategy registry.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.technical_analysis.strategy_registry import get_strategy_registry

from .constants import CALL, PUT, FLAT


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
        # legacy â€” kept for any unreferenced test code
        "call_agree": positive_votes >= 2,
        "put_agree": negative_votes >= 2,
        "any_call_tailwind": positive_votes >= 1,
        "any_put_tailwind": negative_votes >= 1,
        "weighted_call_tilt": weighted_mean >= GLOBAL_WEIGHTED_TILT_THRESHOLD,
        "weighted_put_tilt": weighted_mean <= -GLOBAL_WEIGHTED_TILT_THRESHOLD,
        # active variants
        "all_neg": negative_votes >= 3,   # all 3 regions negative â€” suppress CALL
        "all_pos": positive_votes >= 3,   # all 3 regions positive â€” suppress PUT
        "asia_neg": asia < 0,              # Asia negative â€” suppress CALL
        "asia_pos": asia > 0,              # Asia positive â€” suppress PUT
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


def _strategy_threshold(df: pd.DataFrame, key: str) -> pd.Series:
    """Build a per-row Series from the shared strategy config."""
    from src.common.config import get_strategy_config
    return pd.Series(float(get_strategy_config()[key]), index=df.index)


def pullback_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: dips get bought inside intact trend or deep quiet-volume washout."""
    rp20 = pd.to_numeric(
        df["range_position_20d"] if "range_position_20d" in df.columns
        else pd.Series(np.nan, index=df.index), errors="coerce",
    )
    rp10  = pd.to_numeric(df["range_position_10d"], errors="coerce")
    vix   = pd.to_numeric(df["vix_close"], errors="coerce")
    s20   = pd.to_numeric(df["ma20_slope"], errors="coerce")
    room  = _upside_room(df)
    support_ok = ~_flag(df, "support_broken_10d")
    rsi5  = pd.to_numeric(
        df["rsi5"] if "rsi5" in df.columns else pd.Series(np.nan, index=df.index),
        errors="coerce",
    )
    vix_qmax = _strategy_threshold(df, "vix_quiet_max")
    return {
        "strategy_PullbackCall_DeepWashout_signal":
            _sig((rp20 <= 0.25) & (vix <= vix_qmax) & (rsi5 <= 30), CALL),
        "strategy_PullbackCall_TrendIntact_signal":
            _sig((s20 >= 0.003) & (rp10 <= 0.20) & (room >= 0.015) & support_ok, CALL),
    }


def decline_continuation_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: young ATR-scaled decline extends next session (both regimes)."""
    ret3     = pd.to_numeric(df["ret_3d"], errors="coerce")
    s5       = pd.to_numeric(df["ma5d_slope"], errors="coerce")
    rp10     = pd.to_numeric(df["range_position_10d"], errors="coerce")
    bbw      = pd.to_numeric(df["bb_width"], errors="coerce")
    atr_frac = _atr_pct(df)
    bb_min   = _strategy_threshold(df, "bb_width_min")
    base = (ret3 <= -0.5 * atr_frac) & (s5 < 0) & (rp10 >= 0.20) & (bbw >= bb_min)
    # v2: replace ma5d_slope < 0 with three consecutive lower closes
    close       = pd.to_numeric(df["close_1515"], errors="coerce")
    lower_closes = (close < close.shift(1)) & (close.shift(1) < close.shift(2))
    base_v2 = (ret3 <= -0.5 * atr_frac) & lower_closes & (rp10 >= 0.20) & (bbw >= bb_min)
    return {
        "strategy_DeclineContinuationPut_ATR_signal": _sig(base, PUT),
        "strategy_DeclineContinuationPut_ATR_v2_signal": _sig(base_v2, PUT),
    }


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
    """RESEARCH: two-sided high-vol expansion context signal."""
    raw = _momentum_directional_signals(df)
    strong = raw.get(
        "strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal",
        pd.Series(FLAT, index=df.index),
    )

    return {
        "strategy_ExpansionVotes_Strong_signal": strong,
    }


def bollinger_mean_reversion(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: CALL below lower Bollinger band; PUT above upper band."""
    close = pd.to_numeric(df["close_1515"], errors="coerce")
    lower = pd.to_numeric(df["bb_lower"], errors="coerce")
    upper = pd.to_numeric(df["bb_upper"], errors="coerce")
    return {
        "strategy_BollingerMeanReversion_signal": _two_sided_signal(
            close < lower,
            close > upper,
        )
    }


def macd_ema5_20(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: EMA5-EMA20 zero-line cross."""
    close = pd.to_numeric(df["close_1515"], errors="coerce")
    ema5 = close.ewm(span=5, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    spread = ema5 - ema20
    prev = spread.shift(1)
    return {
        "strategy_MACD_EMA5_20_signal": _two_sided_signal(
            (prev <= 0) & (spread > 0),
            (prev >= 0) & (spread < 0),
        )
    }


def breakdown_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: PUT at/below prior 20-session low with expansion + broken support."""
    put_base = _range_breakout_put(df)
    return {"strategy_BreakdownPut_20d_signal": put_base}


def rsi_reversion(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: RSI oversold/overbought mean-reversion."""
    rsi = df["rsi14"]
    sig = np.where(rsi <= 40.0, CALL, np.where(rsi >= 60.0, PUT, FLAT))
    return {"strategy_RsiReversion_6040_signal": pd.Series(sig, index=df.index)}


def drift_probe_put(df: pd.DataFrame) -> dict[str, pd.Series]:
    """SIGNAL: PUT only — fires when the 5-minute opening drift is sufficiently negative."""
    from src.common.config import get_drift_probe_min_pct

    drift = pd.to_numeric(
        df["nifty_drift_pct"] if "nifty_drift_pct" in df.columns else pd.Series(np.nan, index=df.index),
        errors="coerce",
    )
    threshold = get_drift_probe_min_pct()
    sig = np.where(drift <= -threshold, PUT, FLAT)
    return {"strategy_DRIFT_PROBE_PUT_signal": pd.Series(sig, index=df.index)}


def _numeric_col(df: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(default, index=df.index, dtype=float)


def _string_col(df: pd.DataFrame, column: str, default: str = "") -> pd.Series:
    if column in df.columns:
        return df[column].astype(str)
    return pd.Series(default, index=df.index, dtype=object)


def nifty_2026_call_put_research(df: pd.DataFrame) -> dict[str, pd.Series]:
    """RESEARCH: revised six-strategy rules with explicit next-day gap gates."""
    close = _numeric_col(df, "close_1515")
    open_915 = _numeric_col(df, "open_915")
    ma20 = _numeric_col(df, "ma20")
    ma50 = _numeric_col(df, "ma50")
    ma20_slope = _numeric_col(df, "ma20_slope")
    rsi5 = _numeric_col(df, "rsi5")
    ret3 = _numeric_col(df, "ret_3d")
    ret5 = _numeric_col(df, "ret_5d")
    atr14 = _numeric_col(df, "atr14")
    vix_close = _numeric_col(df, "vix_close")
    rp20 = _numeric_col(df, "range_position_20d")
    z20 = _numeric_col(df, "price_zscore_20d")
    pcr_oi = _numeric_col(df, "pcr_oi")
    high_day = _numeric_col(df, "high_day")
    low_day = _numeric_col(df, "low_day")
    close_1415 = _numeric_col(df, "close_1415")
    price_1415 = _numeric_col(df, "price_1415")
    support_dist = _numeric_col(df, "support_distance_10d")
    oi_regime = _string_col(df, "oi_regime", default="")
    gap_atr = _numeric_col(df, "gap_open_atr")
    gap_pct = _numeric_col(df, "nifty_gap_pct")

    selloff_atr = (close - open_915) / atr14.replace(0, np.nan)
    stretched_up = (z20 > 1.5) | (rsi5 > 75)
    trend_pullback_rsi_ok = (rsi5 < 40) | ((rsi5 >= 40) & (rsi5 <= 50) & (oi_regime == "long_unwinding"))
    day_range = (high_day - low_day).replace(0, np.nan)
    close_bottom_half = ((close - low_day) / day_range) <= 0.5
    ref_1415 = close_1415.combine_first(price_1415)
    late_selloff_continuation = close <= (ref_1415 - (0.15 * atr14))

    out: dict[str, pd.Series] = {
        # CALL strategy 1: TrendPullbackCall.
        "strategy_TrendPullbackCall_signal": _sig(
            (close > ma50)
            & (ma20_slope > 0)
            & trend_pullback_rsi_ok
            & (ret3 <= -0.005)
            & (vix_close >= 13)
            & (oi_regime != "long_buildup")
            & (gap_atr <= 0.25),
            CALL,
        ),

        # CALL strategy 2: DowntrendBounceCall.
        "strategy_DowntrendBounceCall_signal": _sig(
            (close < ma20)
            & (ma20_slope < 0)
            & (rsi5 < 30)
            & (selloff_atr <= -0.4)
            & (vix_close >= 12)
            & (gap_atr <= 0.25),
            CALL,
        ),

        # CALL strategy 3: ReversalCall.
        "strategy_ReversalCall_signal": _sig(
            (support_dist.abs() <= 0.75)
            & (rsi5 < 30)
            & (oi_regime == "short_buildup")
            & (vix_close >= 12)
            & (gap_atr <= -0.25),
            CALL,
        ),

        # PUT strategy 1: DowntrendContinuationPut.
        "strategy_DowntrendContinuationPut_signal": _sig(
            (close < ma50)
            & (ma20_slope < 0)
            & (ret5 <= 0.01)
            & (rsi5 >= 35)
            & (rsi5 <= 55)
            & (vix_close >= 13)
            & (oi_regime != "short_covering")
            & (gap_atr >= -0.25),
            PUT,
        ),

        # PUT strategy 2: UptrendExhaustionPut.
        "strategy_UptrendExhaustionPut_signal": _sig(
            (close >= (ma20 * 1.025))
            & (ma20_slope > 0)
            & stretched_up
            & (pcr_oi <= 1.15)
            & (gap_pct >= 0),
            PUT,
        ),

        # PUT strategy 3: ReversalPut.
        "strategy_ReversalPut_signal": _sig(
            (rp20 >= 0.8)
            & (rsi5 > 70)
            & (pcr_oi <= 1.15)
            & close_bottom_half
            & late_selloff_continuation,
            PUT,
        ),
    }
    return out


def macro_direction(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Stateful two-sided macro vote with entry and half-threshold exit bands."""
    threshold = 0.75
    exit_threshold = 0.5 * threshold
    scores = _numeric_col(df, "macro_risk_off")
    votes: list[str] = []
    state = FLAT

    for score in scores:
        if pd.isna(score):
            state = FLAT
        elif state == PUT:
            if score >= exit_threshold:
                state = PUT
            elif score <= -threshold:
                state = CALL
            else:
                state = FLAT
        elif state == CALL:
            if score <= -exit_threshold:
                state = CALL
            elif score >= threshold:
                state = PUT
            else:
                state = FLAT
        elif score >= threshold:
            state = PUT
        elif score <= -threshold:
            state = CALL
        else:
            state = FLAT
        votes.append(state)

    return {
        "strategy_MacroDirection_signal": pd.Series(votes, index=df.index, dtype=object)
    }


_STRATEGY_GENERATORS = {
    "BollingerMeanReversion": bollinger_mean_reversion,
    "PullbackCall":           pullback_call,
    "DeclineContinuationPut": decline_continuation_put,
    "BreakdownPut":           breakdown_put,
    "RsiReversion":           rsi_reversion,
    "DriftProbePut":          drift_probe_put,
    # The six 2026 call/put rules are emitted by one function and filtered by
    # strategy_type via the registry, so they can be promoted/demoted from YAML.
    "Nifty2026CallPut":       nifty_2026_call_put_research,
}


def _filter_by_strategy_type(fn, allowed_types: set[str]):
    def _filtered(df: pd.DataFrame) -> dict[str, pd.Series]:
        registry = get_strategy_registry()
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


PROMOTED_STRATEGIES = {
    name: _filter_by_strategy_type(fn, {"SIGNAL"})
    for name, fn in _STRATEGY_GENERATORS.items()
}

# All participating production strategies.
ALL_PRODUCTION_STRATEGIES = {
    name: _filter_by_strategy_type(fn, {"SIGNAL"})
    for name, fn in _STRATEGY_GENERATORS.items()
}

# Human-readable definitions for the promoted strategies, keyed by metric name
# (signal column without the strategy_ prefix and _signal suffix).
PROMOTED_DEFINITIONS: dict[str, str] = {}
