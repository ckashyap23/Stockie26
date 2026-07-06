from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.technical_analysis.cascade.constants import (
    CALL, PUT, FLAT,
    REGIME_CALM, REGIME_STRESS,
    REGIME_VIX_CUTOFF, REGIME_VOL_CUTOFF,
)
from src.technical_analysis.cascade.dataset import build_base
from src.technical_analysis.cascade.strategies import (
    calm_fade_put,
    calm_momentum_call,
    calm_momentum_put,
    calm_trend_call,
    down_momentum_put,
    mean_reversion,
    momentum_directional,
    oversold_bounce_call,
    range_breakout,
    stress_watch_candidates,
    _regional_components,
)
from src.technical_analysis.strategy_families import get_strategy_family_registry
from src.technical_analysis.cascade.watch_promotion import add_watch_promotions

SignalFn = Callable[[pd.DataFrame], pd.Series]
FamilyFn = Callable[[pd.DataFrame], dict[str, pd.Series]]


@dataclass(frozen=True)
class StrategyVariant:
    name: str
    signal_fn: SignalFn
    description: str


def cascade_variant(
    name: str,
    family_fn: FamilyFn,
    signal_key: str,
    description: str = "",
) -> StrategyVariant:
    """Wrap a cascade family function (returns dict[keyâ†’Series]) into a StrategyVariant.

    family_fn  â€” any function from cascade.strategies returning dict[str, pd.Series].
    signal_key â€” the exact dict key to extract (e.g. "strategy_OversoldBounceCall_ContextRoom_signal").

    NOTE: The family function's selected_names filter may exclude some global variant keys.
    Use cascade_global_variant() instead for GlobalAllDisagree / GlobalAsiaDisagree variants.
    """
    def signal(df: pd.DataFrame) -> pd.Series:
        result = family_fn(df)
        if signal_key in result:
            return result[signal_key]
        # Key was filtered out by selected_names — return FLAT rather than raising KeyError
        return pd.Series(FLAT, index=df.index)
    return StrategyVariant(name=name, signal_fn=signal, description=description or signal_key)


def cascade_global_variant(
    name: str,
    family_fn: FamilyFn,
    base_signal_key: str,
    mode: str,  # "all" = GlobalAllDisagree, "asia" = GlobalAsiaDisagree
    description: str = "",
) -> StrategyVariant:
    """Build a global-filter variant independently of the family's selected_names.

    Extracts the base signal by key from the family dict (falling back to any matching key),
    then applies the suppression mask directly — so it works even when the GlobalXxx key
    has been removed from the production selected_names roster.
    """
    def signal(df: pd.DataFrame) -> pd.Series:
        result = family_fn(df)
        # Try exact key first; fall back to the base (non-global) signal if present
        if base_signal_key in result:
            base = result[base_signal_key]
        else:
            # derive the base key: strip the _GlobalXxxDisagree suffix
            for suffix in ("_GlobalAllDisagree_signal", "_GlobalAsiaDisagree_signal"):
                trimmed = base_signal_key.replace(suffix, "_signal")
                if trimmed in result:
                    base = result[trimmed]
                    break
            else:
                return pd.Series(FLAT, index=df.index)

        regional = _regional_components(df)
        if mode == "all":
            suppressed = (
                ((base == CALL) & regional["all_neg"].fillna(False))
                | ((base == PUT) & regional["all_pos"].fillna(False))
            )
        else:  # asia
            suppressed = (
                ((base == CALL) & regional["asia_neg"].fillna(False))
                | ((base == PUT) & regional["asia_pos"].fillna(False))
            )
        return base.where(~suppressed, FLAT)

    return StrategyVariant(name=name, signal_fn=signal, description=description)


def _sig(mask: pd.Series, side: str) -> pd.Series:
    return pd.Series(np.where(mask.fillna(False), side, FLAT), index=mask.index)


def _add_regime_column(df: pd.DataFrame) -> pd.DataFrame:
    """Add 'regime' column using same VIX/vol thresholds as the production cascade.

    calm  = vix_close < 13  AND  volatility_10d < 0.007
    stress = everything else
    """
    calm = (
        pd.to_numeric(df["vix_close"], errors="coerce") < REGIME_VIX_CUTOFF
    ) & (
        pd.to_numeric(df["volatility_10d"], errors="coerce") < REGIME_VOL_CUTOFF
    )
    df = df.copy()
    df["regime"] = np.where(calm.fillna(False), REGIME_CALM, REGIME_STRESS)
    return df


def _gate_to_regime(signal_fn: SignalFn, regime: str) -> SignalFn:
    """Suppress a signal to FLAT on dates that don't match the target regime."""
    def gated(df: pd.DataFrame) -> pd.Series:
        sig = signal_fn(df)
        if "regime" in df.columns:
            wrong_regime = df["regime"] != regime
            return sig.where(~wrong_regime, FLAT)
        return sig
    return gated


def ma_spread_variant(name: str, spread_threshold: float, rsi_call_max: float, rsi_put_min: float) -> StrategyVariant:
    def signal(df: pd.DataFrame) -> pd.Series:
        spread = (df["ma10"] - df["ma20"]) / df["ma20"]
        call = (spread > spread_threshold) & (df["rsi14"] <= rsi_call_max)
        put = (spread < -spread_threshold) & (df["rsi14"] >= rsi_put_min)
        return _two_sided_signal(call, put, df.index)

    return StrategyVariant(
        name=name,
        signal_fn=signal,
        description=(
            f"MA10/MA20 spread threshold {spread_threshold:.4f}; "
            f"CALL if RSI <= {rsi_call_max:g}, PUT if RSI >= {rsi_put_min:g}."
        ),
    )


def rsi_reversion_variant(name: str, low: float, high: float) -> StrategyVariant:
    def signal(df: pd.DataFrame) -> pd.Series:
        return pd.Series(
            np.where(df["rsi14"] <= low, CALL, np.where(df["rsi14"] >= high, PUT, FLAT)),
            index=df.index,
        )

    return StrategyVariant(
        name=name,
        signal_fn=signal,
        description=f"CALL when RSI14 <= {low:g}; PUT when RSI14 >= {high:g}.",
    )


def room_alignment_variant(name: str, room_min: float, support_min: float, rsi_call_max: float, rsi_put_min: float) -> StrategyVariant:
    def signal(df: pd.DataFrame) -> pd.Series:
        close = df["close_1515"].astype(float)
        ma5 = close.rolling(5).mean()
        spread = (df["ma10"] - df["ma20"]) / df["ma20"]
        call = (
            (ma5 > df["ma10"])
            & (spread > 0)
            & (df["rsi14"] <= rsi_call_max)
            & (df["resistance_distance_10d"] >= room_min)
        )
        put = (
            (ma5 < df["ma10"])
            & (spread < 0)
            & (df["rsi14"] >= rsi_put_min)
            & (df["support_distance_10d"] >= support_min)
        )
        return _two_sided_signal(call, put, df.index)

    return StrategyVariant(
        name=name,
        signal_fn=signal,
        description=(
            f"MA5/MA10 aligned with MA10/MA20 spread, room >= {room_min:.3f}, "
            f"support room >= {support_min:.3f}, RSI CALL <= {rsi_call_max:g}, PUT >= {rsi_put_min:g}."
        ),
    )


def macd_variant(name: str, fast_span: int, slow_span: int) -> StrategyVariant:
    def signal(df: pd.DataFrame) -> pd.Series:
        close = pd.to_numeric(df["close_1515"], errors="coerce")
        fast = close.ewm(span=fast_span, adjust=False, min_periods=fast_span).mean()
        slow = close.ewm(span=slow_span, adjust=False, min_periods=slow_span).mean()
        macd = fast - slow
        call = (macd > 0) & (macd.shift(1) <= 0)
        put = (macd < 0) & (macd.shift(1) >= 0)
        return _two_sided_signal(call, put, df.index)

    return StrategyVariant(
        name=name,
        signal_fn=signal,
        description=f"MACD-style EMA crossover: CALL when EMA{fast_span}-EMA{slow_span} crosses above zero; PUT when it crosses below zero.",
    )


# ---------------------------------------------------------------------------
# RESEARCH VARIANTS
# ---------------------------------------------------------------------------
# All strategies tracked in the research grid. Two sub-groups are annotated
# in descriptions for clarity:
#
#   [PRODUCTION] — variant is part of the live production cascade (passes
#                  the precision floor; defined in PROMOTED_REGIME_FAMILIES).
#                  Name is identical to production primary_strategy label.
#
#   [RESEARCH]   — variant does NOT participate in production (below floor,
#                  comparison baseline, or experimental). Research-only.
#
# The production cascade itself is the single source of truth for what is
# "promoted" — that lives in src/technical_analysis/cascade/strategies.py.
# ---------------------------------------------------------------------------

def _momentum_directional_call_guard(df: pd.DataFrame) -> pd.Series:
    base = momentum_directional(df)["strategy_MomentumDirectional_signal"]
    return _sig((base == CALL) & (df["bb_width"] >= 0.055), CALL)


def _momentum_directional_two_sided(df: pd.DataFrame) -> pd.Series:
    return momentum_directional(df)["strategy_MomentumDirectional_signal"]


def _range_breakdown_put_global_all_disagree(df: pd.DataFrame) -> pd.Series:
    """Research definition aligned with production: PUT-only, never CALL."""
    signal = range_breakout(df)["strategy_RangeBreakoutPut_GlobalAllDisagree_signal"]
    return signal.where(signal == PUT, FLAT)


RESEARCH_VARIANTS: list[StrategyVariant] = [
    # ── OversoldBounceCall ─────────────────────────────────────────────────
    cascade_variant("OversoldBounceCall_HighPrecision", oversold_bounce_call,
        "strategy_OversoldBounceCall_HighPrecision_signal",
        "[PRODUCTION] CALL range_position_10d<=20th pctile, vix>=12."),
    cascade_variant("OversoldBounceCall_MoreTrades", oversold_bounce_call,
        "strategy_OversoldBounceCall_MoreTrades_signal",
        "[PRODUCTION] CALL rsi14<=42, room>=2.5%, vix>=12. No global filter (filter hurts precision)."),
    cascade_variant("OversoldBounceCall_ContextRoom", oversold_bounce_call,
        "strategy_OversoldBounceCall_ContextRoom_signal",
        "[RESEARCH] CALL dynamic rsi cap + dynamic room floor. Below 0.70 precision floor."),
    # ── DownMomentumPut ───────────────────────────────────────────────────
    cascade_variant("DownMomentumPut_HighPrecision", down_momentum_put,
        "strategy_DownMomentumPut_HighPrecision_signal",
        "[PRODUCTION] PUT ma20_slope<=-0.3%, volume floor cleared, vix_chg_1d>0."),
    cascade_variant("DownMomentumPut_HighPrecision_GlobalAllDisagree", down_momentum_put,
        "strategy_DownMomentumPut_HighPrecision_GlobalAllDisagree_signal",
        "[PRODUCTION] DownMomentumPut_HighPrecision suppressed when all 3 global regions positive."),
    cascade_variant("DownMomentumPut_MoreTrades", down_momentum_put,
        "strategy_DownMomentumPut_MoreTrades_signal",
        "[PRODUCTION] PUT ma20_slope<=-0.3%, volume floor cleared, vix>=12. No global filter."),
    # ── MomentumDirectional ContextVotes ──────────────────────────────────
    cascade_variant("MomentumDirectional_ContextVotes_StrongExpansionGuard", momentum_directional,
        "strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal",
        "[PRODUCTION] Context vote two-sided: vix>=16 and bb_width>=6.5%."),
    cascade_variant("MomentumDirectional_ContextVotes_CallExpansionGuard_GlobalAsiaDisagree", momentum_directional,
        "strategy_MomentumDirectional_ContextVotes_CallExpansionGuard_GlobalAsiaDisagree_signal",
        "[PRODUCTION] MomentumDirectional CallExpansionGuard CALL suppressed when Asia region negative."),
    StrategyVariant(name="MomentumDirectional",
        signal_fn=_momentum_directional_two_sided,
        description="[RESEARCH] Two-sided base: CALL >=2 votes / PUT >=3 votes. Comparison baseline."),
    # ── BollingerMeanReversion ────────────────────────────────────────────
    cascade_variant("BollingerMeanReversion", mean_reversion,
        "strategy_BollingerMeanReversion_signal",
        "[PRODUCTION] CALL close < lower Bollinger band; PUT close > upper band."),
    cascade_variant("BollingerMeanReversion_RelaxedVolWatch", mean_reversion,
        "strategy_BollingerMeanReversion_RelaxedVolWatch_signal",
        "[WATCH_ONLY] Band breach with VIX>=10 or BB width>=4.5%, excluding severe adverse trend."),
    cascade_variant("BollingerMeanReversion_BorderlineTrendWatch", mean_reversion,
        "strategy_BollingerMeanReversion_BorderlineTrendWatch_signal",
        "[WATCH_ONLY] Band breach in a borderline, but not severe, adverse trend."),
    cascade_variant("BollingerMeanReversion_BandProximityWatch", mean_reversion,
        "strategy_BollingerMeanReversion_BandProximityWatch_signal",
        "[WATCH_ONLY] Within 0.25% inside a band with RSI5 and relaxed-vol confirmation."),
    # ── RsiMeanReversion ──────────────────────────────────────────────────
    cascade_variant("RsiMeanReversion_6040", mean_reversion,
        "strategy_RsiMeanReversion_6040_signal",
        "[RESEARCH] CALL rsi14<40; PUT rsi14>60. Below 0.70 precision floor."),
    # ── RangeBreakout ─────────────────────────────────────────────────────
    StrategyVariant(name="RangeBreakoutPut_GlobalAllDisagree",
        signal_fn=_range_breakdown_put_global_all_disagree,
        description="[WATCH_ONLY] PUT below the prior 20-session low with BB width >= 6.5%; CALL creation is blocked."),
    cascade_variant("StressOverboughtFadePut_HighPrecision", stress_watch_candidates,
        "strategy_StressOverboughtFadePut_HighPrecision_signal",
        "[WATCH_ONLY] Stress upper-range/RSI5 overbought PUT reversal setup."),
    cascade_variant("UpMomentumCall_HighPrecision", stress_watch_candidates,
        "strategy_UpMomentumCall_HighPrecision_signal",
        "[WATCH_ONLY] Stress upside continuation watch."),
    cascade_variant("RangeBreakoutCall_GlobalRiskAgree", stress_watch_candidates,
        "strategy_RangeBreakoutCall_GlobalRiskAgree_signal",
        "[DIAGNOSTIC_ONLY] Stress upside 20D breakout; not eligible to create or confirm a production watch."),
    # ── CalmTrendCall ─────────────────────────────────────────────────────
    cascade_variant("CalmTrendCall_Headroom", calm_trend_call,
        "strategy_CalmTrendCall_Headroom_signal",
        "[PRODUCTION] CALL bb_width>=4%, ma20_slope>0, room>=1.5%, ma10d_slope<=0 (dip inside uptrend)."),
    cascade_variant("CalmTrendCall_Pullback", calm_trend_call,
        "strategy_CalmTrendCall_Pullback_signal",
        "[PRODUCTION] CALL bb_width>=4%, ma20_slope>0, range_position_10d<=0.5, trend_efficiency>=0.25."),
    # ── CalmFadePut ───────────────────────────────────────────────────────
    cascade_variant("CalmFadePut_Overbought", calm_fade_put,
        "strategy_CalmFadePut_Overbought_signal",
        "[PRODUCTION] PUT bb_width>=4%, rsi14>=65 and rsi5>=80 (multi-horizon exhaustion in calm tape)."),
    cascade_variant("CalmFadePut_ContextOverbought", calm_fade_put,
        "strategy_CalmFadePut_ContextOverbought_signal",
        "[PRODUCTION] PUT rsi14>=rolling 75th-pctile cap and rsi5>=rolling 80th-pctile cap."),
    cascade_variant("CalmFadePut_Overbought_GlobalAsiaDisagree", calm_fade_put,
        "strategy_CalmFadePut_Overbought_GlobalAsiaDisagree_signal",
        "[PRODUCTION] CalmFadePut_Overbought suppressed when Asia region positive."),
    # ── CalmMomentumPut ───────────────────────────────────────────────────
    cascade_variant("CalmMomentumPut_Continuation", calm_momentum_put,
        "strategy_CalmMomentumPut_Continuation_signal",
        "[PRODUCTION] PUT bb_width>=4%, ret_3d<=-0.3% (momentum continuation in calm tape)."),
    cascade_variant("CalmMomentumPut_Continuation_GlobalAllDisagree", calm_momentum_put,
        "strategy_CalmMomentumPut_Continuation_GlobalAllDisagree_signal",
        "[PRODUCTION] CalmMomentumPut_Continuation suppressed when all 3 global regions positive."),
    cascade_variant("CalmMomentumPut_Continuation_GlobalAsiaDisagree", calm_momentum_put,
        "strategy_CalmMomentumPut_Continuation_GlobalAsiaDisagree_signal",
        "[PRODUCTION] CalmMomentumPut_Continuation suppressed when Asia region positive."),
    cascade_variant("CalmMomentumPut_LightContinuationWatch", calm_momentum_put,
        "strategy_CalmMomentumPut_LightContinuationWatch_signal",
        "[WATCH_ONLY] Relaxed calm downside continuation."),
    cascade_variant("CalmMomentumPut_PullbackContinuationWatch", calm_momentum_put,
        "strategy_CalmMomentumPut_PullbackContinuationWatch_signal",
        "[WATCH_ONLY] Calm downside continuation after a failed bounce."),
    # ── CalmMomentumCall ──────────────────────────────────────────────────
    cascade_variant("CalmMomentumCall_Continuation", calm_momentum_call,
        "strategy_CalmMomentumCall_Continuation_signal",
        "[WATCH_ONLY] Calm CALL continuation with bb_width>=4%, positive 3-day return and short slopes."),
    cascade_variant("CalmMomentumCall_Continuation_GlobalAsiaAgree", calm_momentum_call,
        "strategy_CalmMomentumCall_Continuation_GlobalAsiaAgree_signal",
        "[WATCH_ONLY] CalmMomentumCall continuation requiring positive Asia return."),
    cascade_variant("CalmMomentumCall_LightContinuationWatch", calm_momentum_call,
        "strategy_CalmMomentumCall_LightContinuationWatch_signal",
        "[WATCH_ONLY] Relaxed calm upside continuation."),
    cascade_variant("CalmMomentumCall_PullbackContinuationWatch", calm_momentum_call,
        "strategy_CalmMomentumCall_PullbackContinuationWatch_signal",
        "[WATCH_ONLY] Calm upside continuation after a shallow pullback."),
    # ── Simple parametric (research baselines) ────────────────────────────
    macd_variant("MACD_EMA5_20", 5, 20),
]

# Aliases kept for backward compatibility with any external callers.
PROMOTED_VARIANTS: list[StrategyVariant] = [v for v in RESEARCH_VARIANTS if v.description.startswith("[PRODUCTION]")]
EXPERIMENTAL_VARIANTS: list[StrategyVariant] = [v for v in RESEARCH_VARIANTS if v.description.startswith("[RESEARCH]")]
DEFAULT_VARIANTS: list[StrategyVariant] = RESEARCH_VARIANTS


def strategy_definition_rows(
    variants: list[StrategyVariant] = DEFAULT_VARIANTS,
) -> list[dict]:
    registry = get_strategy_family_registry()
    rows = []
    for variant in variants:
        meta = registry.get_meta(variant.name)
        rows.append({
            "strategy_variant": variant.name,
            "strategy_family": meta.family,
            "strategy_type": meta.strategy_type,
            "direction": meta.direction,
            "description": meta.definition or variant.description,
        })
    return rows


def export_strategy_definitions(output_dir: Path, variants: list[StrategyVariant] = DEFAULT_VARIANTS) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "strategy_grid_definitions.csv"
    pd.DataFrame(strategy_definition_rows(variants)).to_csv(path, index=False)
    return path


def build_signal_matrices(
    plans: pd.DataFrame,
    snapshots: pd.DataFrame,
    entry_mode: str = "replay_open",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build VectorBT price/entry/exit matrices from option snapshot replay data.

    For each trade in plans, snapshots provide intraday prices on the replay date.
    Entry is at the first snapshot (entry_mode='replay_open'). Exit is triggered
    when price hits target_1_price or stop_loss_price; otherwise the last snapshot
    (15:15 market close) acts as TIME_EXIT.

    Returns three DataFrames with trade_id columns and snapshot_time index:
        price   â€” option price at each snapshot (ffill filled)
        entries â€” True at the entry snapshot
        exits   â€” True at the exit snapshot
    """
    if plans.empty or snapshots.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    plan_map = plans.set_index("trade_id").to_dict("index")
    trade_ids = list(plan_map.keys())

    all_times: set[pd.Timestamp] = set()
    exit_map: dict[str, dict] = {}

    for tid in trade_ids:
        plan = plan_map[tid]
        snaps = snapshots[snapshots["trade_id"] == tid].sort_values("snapshot_time")
        if snaps.empty:
            continue

        entry_time = pd.Timestamp(snaps.iloc[0]["snapshot_time"])
        entry_price = float(plan.get("primary_buy_entry_price") or plan.get("entry_price") or snaps.iloc[0]["price"])
        target = plan.get("target_1_price")
        stop = plan.get("stop_loss_price") if plan.get("stop_loss_enabled") else None

        exit_time = pd.Timestamp(snaps.iloc[-1]["snapshot_time"])
        exit_price = float(snaps.iloc[-1]["price"])

        for _, snap in snaps.iterrows():
            p = float(snap["price"])
            t = pd.Timestamp(snap["snapshot_time"])
            if target is not None and p >= float(target):
                exit_time, exit_price = t, p
                break
            if stop is not None and p <= float(stop):
                exit_time, exit_price = t, p
                break

        all_times.update([entry_time, exit_time])
        exit_map[tid] = {
            "entry_time": entry_time, "entry_price": entry_price,
            "exit_time": exit_time, "exit_price": exit_price,
        }

    if not exit_map:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    idx = pd.DatetimeIndex(sorted(all_times))
    price = pd.DataFrame(index=idx, columns=list(exit_map.keys()), dtype=float)
    entries_df = pd.DataFrame(False, index=idx, columns=list(exit_map.keys()))
    exits_df = pd.DataFrame(False, index=idx, columns=list(exit_map.keys()))

    for tid, m in exit_map.items():
        price.loc[m["entry_time"], tid] = m["entry_price"]
        price.loc[m["exit_time"], tid] = m["exit_price"]
        price[tid] = price[tid].ffill().bfill()
        entries_df.loc[m["entry_time"], tid] = True
        exits_df.loc[m["exit_time"], tid] = True

    return price, entries_df, exits_df


def build_replay_trades(
    plans: pd.DataFrame,
    snapshots: pd.DataFrame,
    fees: float = 0.0,
    slippage: float = 0.0,
) -> pd.DataFrame:
    if plans.empty or snapshots.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for plan in plans.to_dict("records"):
        trade_id = str(plan.get("trade_id"))
        snaps = snapshots[snapshots["trade_id"].astype(str) == trade_id].sort_values("snapshot_time")
        if snaps.empty:
            continue

        entry_time = pd.Timestamp(snaps.iloc[0]["snapshot_time"])
        entry_price = float(plan.get("primary_buy_entry_price") or plan.get("entry_price") or snaps.iloc[0]["price"])
        target = _float_or_none(plan.get("target_1_price"))
        stop = _float_or_none(plan.get("stop_loss_price")) if plan.get("stop_loss_enabled") else None
        exit_time = pd.Timestamp(snaps.iloc[-1]["snapshot_time"])
        exit_price = float(snaps.iloc[-1]["price"])
        exit_reason = "TIME_EXIT"

        for _, snap in snaps.iloc[1:].iterrows():
            price = float(snap["price"])
            timestamp = pd.Timestamp(snap["snapshot_time"])
            if target is not None and price >= target:
                exit_time, exit_price, exit_reason = timestamp, price, "TARGET_1"
                break
            if stop is not None and price <= stop:
                exit_time, exit_price, exit_reason = timestamp, price, "STOP_LOSS"
                break

        fill_entry = entry_price * (1 + slippage)
        fill_exit = exit_price * (1 - slippage)
        fee_cost = (fill_entry + fill_exit) * fees
        pnl_per_unit = (fill_exit - fill_entry) - fee_cost
        lot_size = _float_or_none(snaps["lot_size"].dropna().iloc[0]) if snaps["lot_size"].notna().any() else None
        rows.append({
            "trade_id": trade_id,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_price": round(fill_entry, 4),
            "exit_price": round(fill_exit, 4),
            "exit_reason": exit_reason,
            "pnl_per_unit": round(pnl_per_unit, 4),
            "pnl_per_lot": round(pnl_per_unit * lot_size, 2) if lot_size else None,
            "return_pct": round(pnl_per_unit / fill_entry * 100, 4) if fill_entry else None,
        })

    return pd.DataFrame(rows)


def run_strategy_grid(
    start: date | None = None,
    end: date | None = None,
    target_pct: float = 0.03,
    target_pcts: list[float] | None = None,
    stop_loss_pct: float | None = None,
    stop_loss_pcts: list[float | None] | None = None,
    initial_cash: float = 100_000.0,
    fees: float = 0.0,
    slippage: float = 0.0,
    output_dir: Path = Path("output") / "backtest" / "NIFTY" / "vectorbt_research",
    variants: list[StrategyVariant] | None = None,
) -> dict[str, Path]:
    variants = variants or DEFAULT_VARIANTS
    target_grid = target_pcts or [target_pct]
    stop_loss_grid = stop_loss_pcts or [stop_loss_pct]
    from src.technical_analysis.prediction.signal_strength import add_raw_direction

    base = add_raw_direction(_add_regime_column(build_base()))
    base["signal_date_dt"] = pd.to_datetime(base["signal_date"]).dt.date
    base = base.reset_index(drop=True)

    eligible_mask = pd.Series(True, index=base.index)
    if start:
        eligible_mask &= base["signal_date_dt"] >= start
    if end:
        eligible_mask &= base["signal_date_dt"] <= end

    all_plans: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []
    leaderboard: list[dict] = []
    definitions: list[dict] = []
    watch_promotion_rows: list[pd.DataFrame] = []

    for variant in variants:
        signal = variant.signal_fn(base)
        eligible = base.loc[eligible_mask].copy().reset_index(drop=True)
        eligible_signal = signal.loc[eligible_mask].reset_index(drop=True)
        promotion_signal, promotion_rows = watch_promotion_attribution(
            eligible, eligible_signal, variant.name
        )
        if not promotion_rows.empty:
            watch_promotion_rows.append(promotion_rows)
        for target_value in target_grid:
            for stop_value in stop_loss_grid:
                plans = build_atm_option_trade_plans(eligible, eligible_signal, variant.name, target_value, stop_value)
                snapshots = load_replay_snapshots(plans)
                trades = build_replay_trades(plans, snapshots, fees=fees, slippage=slippage)
                enriched = enrich_grid_trades(trades, plans, snapshots, used_vectorbt=False)
                if not enriched.empty:
                    enriched["strategy_variant"] = variant.name
                    # Enrich with actual_trade_label from base for win/loss diagnosis
                    if "actual_trade_label" in base.columns:
                        label_map = (
                            base[["signal_date_dt", "actual_trade_label"]]
                            .drop_duplicates("signal_date_dt")
                            .rename(columns={"signal_date_dt": "signal_date"})
                        )
                        enriched = enriched.merge(label_map, on="signal_date", how="left")
                    all_trades.append(enriched)
                if not plans.empty:
                    all_plans.append(plans)
                leaderboard.append(leaderboard_row(variant.name, {}, enriched, plans, target_value, stop_value,
                                                   eligible=eligible, eligible_signal=eligible_signal,
                                                   watch_promotion_signal=promotion_signal))
        definitions.extend(strategy_definition_rows([variant]))

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "leaderboard": output_dir / "strategy_grid_leaderboard.csv",
        "trades": output_dir / "strategy_grid_trades.csv",
        "plans": output_dir / "strategy_grid_trade_plans.csv",
        "definitions": output_dir / "strategy_grid_definitions.csv",
        "watch_promotions": output_dir / "strategy_grid_watch_promotions.csv",
        "summary": output_dir / "strategy_grid_summary.txt",
    }
    pd.DataFrame(leaderboard).sort_values(["total_pnl_per_unit", "win_rate_pct"], ascending=False).to_csv(paths["leaderboard"], index=False)
    pd.concat(all_trades, ignore_index=True).to_csv(paths["trades"], index=False) if all_trades else pd.DataFrame().to_csv(paths["trades"], index=False)
    pd.concat(all_plans, ignore_index=True).to_csv(paths["plans"], index=False) if all_plans else pd.DataFrame().to_csv(paths["plans"], index=False)
    pd.DataFrame(definitions).to_csv(paths["definitions"], index=False)
    (pd.concat(watch_promotion_rows, ignore_index=True) if watch_promotion_rows else pd.DataFrame()).to_csv(
        paths["watch_promotions"], index=False
    )
    write_summary(paths["summary"], leaderboard, definitions)
    return paths


def watch_promotion_attribution(
    df: pd.DataFrame,
    signal: pd.Series,
    strategy: str,
) -> tuple[pd.Series, pd.DataFrame]:
    """Replay one WATCH_ONLY variant and attribute D1/D2 promotions to its D0 origin."""
    meta = get_strategy_family_registry().get_meta(strategy)
    empty_signal = pd.Series(FLAT, index=df.index, dtype=object)
    if meta.strategy_type != "WATCH_ONLY" or df.empty:
        return empty_signal, pd.DataFrame()

    regime_signals = {
        REGIME_STRESS: {strategy: signal},
        REGIME_CALM: {strategy: signal},
    }
    promotions = add_watch_promotions(df, empty_signal, regime_signals)
    promoted = promotions["promoted_prediction"].copy()
    rows: list[dict] = []
    for position, idx in enumerate(df.index):
        direction = promoted.loc[idx]
        if direction not in {CALL, PUT}:
            continue
        age = int(promotions.loc[idx, "prior_watch_age"])
        origin_position = position - age
        rows.append({
            "watch_strategy": strategy,
            "strategy_family": meta.family,
            "strategy_type": meta.strategy_type,
            "watch_signal_date": df.iloc[origin_position]["signal_date"],
            "promotion_signal_date": df.iloc[position]["signal_date"],
            "watch_age": age,
            "promoted_prediction": direction,
            "confirmation_variant": promotions.loc[idx, "confirming_variant"],
            "confirmation_type": promotions.loc[idx, "confirming_strategy_type"],
            "actual_trade_label": df.iloc[position].get("actual_trade_label"),
            "promotion_reason": promotions.loc[idx, "promotion_reason"],
        })
    return promoted, pd.DataFrame(rows)


def build_atm_option_trade_plans(
    df: pd.DataFrame,
    signal: pd.Series,
    strategy_name: str,
    target_pct: float,
    stop_loss_pct: float | None,
) -> pd.DataFrame:
    rows: list[dict] = []
    target_label = f"t{target_pct:g}".replace(".", "p")
    stop_label = "slNone" if stop_loss_pct is None else f"sl{stop_loss_pct:g}".replace(".", "p")
    for idx, row in df.iterrows():
        side = str(signal.iloc[idx])
        if side not in {CALL, PUT}:
            continue
        next_trade_date = _date_or_none(row.get("next_trade_date"))
        if next_trade_date is None:
            continue
        option_type = "CE" if side == CALL else "PE"
        rows.append({
            "trade_id": f"{strategy_name}_{target_label}_{stop_label}_{row['signal_date']}_{option_type}",
            "strategy_variant": strategy_name,
            "signal_date": row["signal_date_dt"],
            "replay_trade_date": next_trade_date,
            "final_prediction": side,
            "direction": side,
            "spot_price": _float_or_none(row.get("close_1515")),
            "option_type": option_type,
            "target_pct": target_pct,
            "stop_loss_pct": stop_loss_pct,
        })
    plans = pd.DataFrame(rows)
    if plans.empty:
        return plans

    selected = load_atm_options_for_plans(plans)
    if selected.empty:
        return selected
    selected["primary_buy_entry_price"] = selected["entry_price"]
    selected["target_1_price"] = selected["entry_price"] * (1 + target_pct)
    selected["target_2_price"] = selected["entry_price"] * (1 + target_pct)
    selected["stop_loss_enabled"] = stop_loss_pct is not None and stop_loss_pct > 0
    selected["stop_loss_price"] = selected["entry_price"] * (1 - stop_loss_pct) if stop_loss_pct else None
    return selected


def load_atm_options_for_plans(plans: pd.DataFrame) -> pd.DataFrame:
    if plans.empty:
        return plans

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        rows: list[dict] = []
        with db.conn.cursor() as cur:
            for plan in plans.itertuples(index=False):
                cur.execute(
                    """
                    SELECT
                        oi.instrument_token,
                        oi.tradingsymbol,
                        oi.strike,
                        oi.expiry,
                        oi.instrument_type,
                        oi.lot_size,
                        os.last_price
                    FROM "OptionSnapshot" os
                    JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
                    JOIN "OptionSnapshotCalc" calc ON calc.option_snapshot_id = os.id
                    WHERE UPPER(oi.underlying) = 'NIFTY'
                      AND oi.instrument_type = %s
                      AND os.trade_date = %s
                      AND os.last_price IS NOT NULL
                      AND os.last_price > 0
                      AND calc.delta IS NOT NULL
                      AND ABS(calc.delta) BETWEEN 0.70 AND 0.90
                    ORDER BY ABS(ABS(calc.delta) - 0.80), oi.expiry, os.snapshot_time
                    LIMIT 1
                    """,
                    (plan.option_type, plan.replay_trade_date),
                )
                row = cur.fetchone()
                if not row:
                    continue
                rows.append({
                    **plan._asdict(),
                    "primary_buy_token": int(row[0]),
                    "primary_buy_symbol": row[1],
                    "primary_buy_strike": float(row[2]) if row[2] is not None else None,
                    "primary_buy_expiry": row[3],
                    "primary_buy_option_type": row[4],
                    "lot_size": int(row[5]) if row[5] is not None else None,
                    "entry_price": float(row[6]),
                })
    finally:
        db.close()
    return pd.DataFrame(rows)


def _trading_dates_window(cur, start_date: date, n: int, underlying: str = "NIFTY") -> list[date]:
    """Return up to n trading dates >= start_date from UnderlyingSnapshot."""
    cur.execute(
        'SELECT trade_date FROM "UnderlyingSnapshot" WHERE underlying=%s AND trade_date >= %s ORDER BY trade_date LIMIT %s',
        (underlying.upper(), start_date, n),
    )
    return [row[0] for row in cur.fetchall()]


def load_replay_snapshots(plans: pd.DataFrame, n_hold_days: int | None = None) -> pd.DataFrame:
    """Load intraday option snapshots for each plan over n_hold_days trading days.

    n_hold_days defaults to TRADE_HORIZON_DAYS env variable (consistent with
    production backtest and live paper trading).
    """
    if plans.empty:
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "trade_date", "price", "lot_size"])

    if n_hold_days is None:
        from src.common.config import get_trade_horizon_days
        n_hold_days = get_trade_horizon_days()

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        frames: list[pd.DataFrame] = []
        with db.conn.cursor() as cur:
            # Cache trading-day windows per unique entry date to avoid repeated queries.
            date_windows: dict[date, list[date]] = {}
            for plan in plans.itertuples(index=False):
                entry_date = plan.replay_trade_date
                if entry_date not in date_windows:
                    date_windows[entry_date] = _trading_dates_window(cur, entry_date, n_hold_days)
                hold_dates = date_windows[entry_date] or [entry_date]
                cur.execute(
                    """
                    SELECT os.trade_date, os.snapshot_time, os.last_price AS price, oi.lot_size
                    FROM "OptionSnapshot" os
                    JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
                    WHERE oi.instrument_token = %s
                      AND os.trade_date = ANY(%s)
                      AND os.last_price IS NOT NULL
                      AND os.last_price > 0
                    ORDER BY os.snapshot_time
                    """,
                    (int(plan.primary_buy_token), hold_dates),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                frame = pd.DataFrame(rows, columns=cols)
                if not frame.empty:
                    frame["trade_id"] = plan.trade_id
                    frames.append(frame)
    finally:
        db.close()
    if not frames:
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "trade_date", "price", "lot_size"])
    out = pd.concat(frames, ignore_index=True)
    out["snapshot_time"] = pd.to_datetime(out["snapshot_time"])
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out["lot_size"] = pd.to_numeric(out["lot_size"], errors="coerce")
    return out.dropna(subset=["price"]).sort_values(["trade_id", "snapshot_time"]).reset_index(drop=True)


def enrich_grid_trades(trades: pd.DataFrame, plans: pd.DataFrame, snapshots: pd.DataFrame, used_vectorbt: bool) -> pd.DataFrame:
    if trades.empty or plans.empty:
        return trades
    out = trades.copy()
    if used_vectorbt and "Column" in out.columns:
        trade_ids = list(plans["trade_id"])
        out["trade_id"] = out["Column"].apply(
            lambda value: trade_ids[int(value)] if str(value).isdigit() and int(value) < len(trade_ids) else str(value)
        )
    plans = plans.copy()
    plans["trade_id"] = plans["trade_id"].astype(str)
    merge_cols = [
        "trade_id", "strategy_variant", "signal_date", "trade_date", "replay_trade_date",
        "final_prediction", "primary_buy_symbol", "primary_buy_token",
        "primary_buy_option_type", "entry_price", "target_pct",
        "stop_loss_pct", "target_1_price", "target_2_price",
        "stop_loss_enabled", "stop_loss_price",
    ]
    out = out.merge(plans[[c for c in merge_cols if c in plans.columns]], on="trade_id", how="left")
    lot_by_trade = (
        snapshots.dropna(subset=["lot_size"])
        .drop_duplicates("trade_id")
        .set_index("trade_id")["lot_size"]
        .to_dict()
        if not snapshots.empty else {}
    )
    out["lot_size"] = out["trade_id"].map(lot_by_trade)
    # Normalise vectorbt column names â†’ internal names used by leaderboard_row.
    # vectorbt uses "Avg Entry Price" / "Avg Exit Price"; fallback uses "pnl_per_unit".
    if "pnl_per_unit" not in out.columns:
        if "Avg Exit Price" in out.columns and "Avg Entry Price" in out.columns:
            out["pnl_per_unit"] = (
                pd.to_numeric(out["Avg Exit Price"], errors="coerce")
                - pd.to_numeric(out["Avg Entry Price"], errors="coerce")
            )
        elif "PnL" in out.columns and "Size" in out.columns:
            out["pnl_per_unit"] = (
                pd.to_numeric(out["PnL"], errors="coerce")
                / pd.to_numeric(out["Size"], errors="coerce")
            )
    if "pnl_per_unit" in out.columns:
        out["pnl_per_lot"] = (
            pd.to_numeric(out["pnl_per_unit"], errors="coerce")
            * pd.to_numeric(out["lot_size"], errors="coerce")
        )
    return out


def leaderboard_row(
    strategy: str,
    metrics: dict,
    trades: pd.DataFrame,
    plans: pd.DataFrame,
    target_pct: float,
    stop_loss_pct: float | None,
    eligible: pd.DataFrame | None = None,
    eligible_signal: pd.Series | None = None,
    watch_promotion_signal: pd.Series | None = None,
) -> dict:
    strategy_meta = get_strategy_family_registry().get_meta(strategy)
    strategy_family = strategy_meta.family
    pnl = pd.to_numeric(trades.get("pnl_per_unit", pd.Series(dtype=float)), errors="coerce").fillna(0)
    pnl_lot = pd.to_numeric(trades.get("pnl_per_lot", pd.Series(dtype=float)), errors="coerce").fillna(0)
    n = len(pnl)
    wins = int((pnl > 0).sum()) if not trades.empty else 0
    losses = int((pnl < 0).sum()) if not trades.empty else 0
    win_rate = round(wins / n * 100, 1) if n else None

    # Direction win rate: did NIFTY touch the target_pct threshold from next_open?
    # Uses regime-aware threshold (stress=0.5%, calm=0.3%) matching production labelling.
    direction_wins = None
    direction_win_rate = None
    if eligible is not None and eligible_signal is not None and not eligible.empty:
        from src.technical_analysis.cascade.constants import REGIME_THRESHOLD, REGIME_STRESS
        sig = eligible_signal.reset_index(drop=True)
        base_r = eligible.reset_index(drop=True)
        fired = sig.isin([CALL, PUT])
        if fired.any():
            fired_rows = base_r.loc[fired].copy()
            fired_sig = sig.loc[fired].reset_index(drop=True)
            fired_rows = fired_rows.reset_index(drop=True)
            o = pd.to_numeric(fired_rows["next_open"], errors="coerce")
            h = pd.to_numeric(fired_rows["next_high"], errors="coerce")
            lo = pd.to_numeric(fired_rows["next_low"], errors="coerce")
            regime_th = fired_rows["regime"].map(
                lambda r: REGIME_THRESHOLD.get(r, REGIME_THRESHOLD[REGIME_STRESS])
            )
            call_hit = (h - o) / o >= regime_th
            put_hit = (o - lo) / o >= regime_th
            is_call = fired_sig == CALL
            is_put = fired_sig == PUT
            dir_correct = (is_call & call_hit) | (is_put & put_hit)
            direction_wins = int(dir_correct.sum())
            direction_win_rate = round(direction_wins / len(fired_rows) * 100, 1)

    from src.technical_analysis.prediction.signal_strength import summarize_signal_quality

    call_fires = put_fires = 0
    call_mean_quality = put_mean_quality = None
    call_pos_quality_pct = put_pos_quality_pct = None
    mean_signal_quality = positive_quality_rate_pct = None

    if eligible is not None and eligible_signal is not None and "raw_signal_quality" in eligible.columns:
        sig = eligible_signal.reset_index(drop=True)
        elig = eligible.reset_index(drop=True)
        call_only = sig.where(sig == CALL, "NO_POSITION")
        put_only  = sig.where(sig == PUT,  "NO_POSITION")
        cq = summarize_signal_quality(call_only, elig)
        pq = summarize_signal_quality(put_only,  elig)
        aq = summarize_signal_quality(sig, elig)
        call_fires         = cq["quality_scored_fires"]
        call_mean_quality  = cq["mean_signal_quality"]
        call_pos_quality_pct = cq["positive_quality_rate_pct"]
        put_fires          = pq["quality_scored_fires"]
        put_mean_quality   = pq["mean_signal_quality"]
        put_pos_quality_pct = pq["positive_quality_rate_pct"]
        mean_signal_quality    = aq["mean_signal_quality"]
        positive_quality_rate_pct = aq["positive_quality_rate_pct"]

    quality_label_result = {
        "qualityBased_precision": None,
        "qualityBased_recall": None,
        "qualityBased_F1": None,
    }
    if eligible is not None and eligible_signal is not None and "actual_quality_label" in eligible.columns:
        from src.technical_analysis.prediction.signal_strength import quality_label_metrics
        quality_label_result = quality_label_metrics(
            eligible_signal.reset_index(drop=True),
            eligible.reset_index(drop=True)["actual_quality_label"],
        )

    actual_trade_label_result = {
        "actualTradeLabel_precision": None,
        "actualTradeLabel_recall": None,
        "actualTradeLabel_F1": None,
    }
    if eligible is not None and eligible_signal is not None:
        from src.technical_analysis.cascade.engine import score_signal
        scored = score_signal(
            eligible.reset_index(drop=True),
            eligible_signal.reset_index(drop=True),
            strategy,
        )
        actual_trade_label_result = {
            "actualTradeLabel_precision": scored.precision if scored.precision == scored.precision else None,
            "actualTradeLabel_recall": scored.recall if scored.recall == scored.recall else None,
            "actualTradeLabel_F1": scored.f1 if scored.f1 == scored.f1 else None,
        }

    watch_promotions = 0
    watch_promotion_precision = None
    watch_promotion_recall = None
    if eligible is not None and watch_promotion_signal is not None:
        promoted = watch_promotion_signal.reset_index(drop=True)
        watch_promotions = int(promoted.isin([CALL, PUT]).sum())
        if watch_promotions:
            from src.technical_analysis.cascade.engine import score_signal
            promotion_score = score_signal(
                eligible.reset_index(drop=True), promoted, f"{strategy}:promoted"
            )
            watch_promotion_precision = (
                promotion_score.precision
                if promotion_score.precision == promotion_score.precision else None
            )
            watch_promotion_recall = (
                promotion_score.recall
                if promotion_score.recall == promotion_score.recall else None
            )

    return {
        "strategy_variant": strategy,
        "strategy_family": strategy_family,
        "strategy_type": strategy_meta.strategy_type,
        "target_pct": target_pct,
        "stop_loss_pct": stop_loss_pct,
        "plans": len(plans),
        "trades": n or int(metrics.get("trades", 0) or 0),
        "direction_wins": direction_wins,
        "direction_win_rate_pct": direction_win_rate,
        "watch_promotions": watch_promotions,
        "watch_promotion_precision": watch_promotion_precision,
        "watch_promotion_recall": watch_promotion_recall,
        **actual_trade_label_result,
        "qualityBased_precision": quality_label_result["qualityBased_precision"],
        "qualityBased_recall": quality_label_result["qualityBased_recall"],
        "qualityBased_F1": quality_label_result["qualityBased_F1"],
        "call_fires": call_fires,
        "call_mean_quality": call_mean_quality,
        "call_pos_quality_pct": call_pos_quality_pct,
        "put_fires": put_fires,
        "put_mean_quality": put_mean_quality,
        "put_pos_quality_pct": put_pos_quality_pct,
        "mean_signal_quality": mean_signal_quality,
        "positive_quality_rate_pct": positive_quality_rate_pct,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "total_pnl_per_unit": round(float(pnl.sum()), 4),
        "total_pnl_per_lot": round(float(pnl_lot.sum()), 2),
        "avg_pnl_per_unit": round(float(pnl.mean()), 4) if n else None,
    }


def write_summary(path: Path, leaderboard: list[dict], definitions: list[dict]) -> None:
    ranked = sorted(leaderboard, key=lambda row: (row.get("total_pnl_per_unit") or 0, row.get("win_rate_pct") or 0), reverse=True)
    lines = ["VectorBT strategy grid summary", "", "Leaderboard:"]
    for row in ranked:
        lines.append(
            f"- {row['strategy_variant']} target={row.get('target_pct')} stop={row.get('stop_loss_pct')}: trades={row['trades']} "
            f"win_rate={row['win_rate_pct']} total_pnl={row['total_pnl_per_unit']}"
        )
    lines += ["", "Definitions:"]
    for item in definitions:
        lines.append(f"- {item['strategy_variant']}: {item['description']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _two_sided_signal(call: pd.Series, put: pd.Series, index) -> pd.Series:
    return pd.Series(np.where(call.fillna(False), CALL, np.where(put.fillna(False), PUT, FLAT)), index=index)


def _date_or_none(value) -> date | None:
    if value is None or pd.isna(value):
        return None
    return pd.to_datetime(value).date()


def _float_or_none(value) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_float_csv(value: str | None) -> list[float] | None:
    if not value:
        return None
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _parse_optional_float_csv(value: str | None) -> list[float | None] | None:
    if not value:
        return None
    out: list[float | None] = []
    for item in value.split(","):
        token = item.strip().lower()
        if not token:
            continue
        out.append(None if token in {"none", "null", "na", ""} else float(token))
    return out or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run quick VectorBT PnL grid for code-defined NIFTY strategy variants.")
    parser.add_argument("--start", default="2026-04-01", help="Start signal date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End signal date YYYY-MM-DD")
    parser.add_argument("--target-pct", type=float, default=0.03)
    parser.add_argument(
        "--target-pcts",
        default=None,
        help="Comma-separated target pct grid, e.g. 0.2,0.3,0.5. Overrides --target-pct.",
    )
    parser.add_argument("--stop-loss-pct", type=float, default=None)
    parser.add_argument(
        "--stop-loss-pcts",
        default=None,
        help="Comma-separated stop-loss pct grid, e.g. none,0.1,0.2. Overrides --stop-loss-pct.",
    )
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--fees", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument(
        "--output-dir",
        default=str(Path("output") / "backtest" / "NIFTY" / "vectorbt_research"),
    )
    parser.add_argument(
        "--variants",
        default=None,
        help=(
            "Comma-separated name substrings to filter variants (case-insensitive). "
            "E.g. --variants Momentum,Rsi  runs only variants whose name contains 'Momentum' or 'Rsi'. "
            "Omit to run all DEFAULT_VARIANTS."
        ),
    )
    parser.add_argument(
        "--definitions-only", action="store_true",
        help="Refresh strategy_grid_definitions.csv without running option replay.",
    )
    args = parser.parse_args()

    selected_variants: list[StrategyVariant] | None = None
    if args.variants:
        filters = [f.strip().lower() for f in args.variants.split(",") if f.strip()]
        selected_variants = [v for v in DEFAULT_VARIANTS if any(f in v.name.lower() for f in filters)]
        if not selected_variants:
            print(f"[WARN] --variants filter '{args.variants}' matched no variants; running all.")
            selected_variants = None

    if args.definitions_only:
        path = export_strategy_definitions(
            Path(args.output_dir), selected_variants or DEFAULT_VARIANTS
        )
        print(f"Definitions written to {path}")
        return

    paths = run_strategy_grid(
        start=date.fromisoformat(args.start) if args.start else None,
        end=date.fromisoformat(args.end) if args.end else None,
        target_pct=args.target_pct,
        target_pcts=_parse_float_csv(args.target_pcts),
        stop_loss_pct=args.stop_loss_pct,
        stop_loss_pcts=_parse_optional_float_csv(args.stop_loss_pcts),
        initial_cash=args.initial_cash,
        fees=args.fees,
        slippage=args.slippage,
        output_dir=Path(args.output_dir),
        variants=selected_variants,
    )
    print({key: str(value) for key, value in paths.items()})


if __name__ == "__main__":
    main()

