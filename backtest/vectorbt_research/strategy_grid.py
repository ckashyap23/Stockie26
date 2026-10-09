from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
import copy
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.technical_analysis.cascade.constants import (
    CALL, PUT, FLAT,
    TARGET_THRESHOLD,
)
from src.technical_analysis.cascade.dataset import build_base
from src.technical_analysis.cascade.strategies import (
    bollinger_mean_reversion,
    breakdown_put,
    decline_continuation_put,
    drift_probe_put,
    macro_direction,
    pullback_call,
    rsi_reversion,
)
from src.technical_analysis.strategy_registry import get_strategy_registry

SignalFn = Callable[[pd.DataFrame], pd.Series]
StrategyFn = Callable[[pd.DataFrame], dict[str, pd.Series]]


@dataclass(frozen=True)
class StrategyVariant:
    name: str
    signal_fn: SignalFn
    description: str


RESEARCH_PREDICTION_COLUMNS = [
    "signal_date",
    "trade_date",
    "strategy_variant",
    "strategy_type",
    "predicted",
    "actual_label",
    "quality_label",
    "max_underlying_up",
    "max_underlying_down",
    "us_ret",
    "europe_ret",
    "asia_ret",
]

RESEARCH_TARGET_PCT_BY_STRATEGY: dict[str, float] = {
    "TrendPullbackCall": 0.006,
    "DowntrendBounceCall": 0.006,
    "ReversalCall": 0.006,
    "DowntrendContinuationPut": 0.006,
    "UptrendExhaustionPut": 0.006,
    "ReversalPut": 0.004,
}

RESEARCH_FIXED_STOP_PCT_BY_STRATEGY: dict[str, float] = {
    "TrendPullbackCall": 0.004,
    "UptrendExhaustionPut": 0.004,
    "ReversalPut": 0.004,
}

RESEARCH_ATR_SCALED_STOP_STRATEGIES: set[str] = {
    "ReversalCall",
    "DowntrendBounceCall",
    "DowntrendContinuationPut",
}


def cascade_variant(
    name: str,
    strategy_fn: StrategyFn,
    signal_key: str,
    description: str = "",
) -> StrategyVariant:
    """Wrap a strategy generator into a research variant.

    strategy_fn returns one or more named signal series.
    signal_key Ã¢â‚¬â€ the exact dict key to extract (e.g. "strategy_OversoldBounceCall_ContextRoom_signal").

    NOTE: A generator's selected_names filter may exclude some global variant keys.
    Use cascade_global_variant() instead for GlobalAllDisagree / GlobalAsiaDisagree variants.
    """
    def signal(df: pd.DataFrame) -> pd.Series:
        result = strategy_fn(df)
        if signal_key in result:
            return result[signal_key]
        # Key was filtered out by selected_names â€” return FLAT rather than raising KeyError
        return pd.Series(FLAT, index=df.index)
    return StrategyVariant(name=name, signal_fn=signal, description=description or signal_key)


def cascade_global_variant(
    name: str,
    strategy_fn: StrategyFn,
    base_signal_key: str,
    mode: str,  # "all" = GlobalAllDisagree, "asia" = GlobalAsiaDisagree
    description: str = "",
) -> StrategyVariant:
    """Build a global-filter variant independently of selected_names.

    Extracts the base signal by key from the generator result,
    then applies the suppression mask directly â€” so it works even when the GlobalXxx key
    has been removed from the production selected_names roster.
    """
    def signal(df: pd.DataFrame) -> pd.Series:
        result = strategy_fn(df)
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


# ---------------------------------------------------------------------------
# RESEARCH VARIANTS
# ---------------------------------------------------------------------------
# Only explicitly approved RESEARCH variants are evaluated here. Production
# SIGNAL strategies are reported through the production backtest and UI.
# ---------------------------------------------------------------------------

RESEARCH_VARIANTS: list[StrategyVariant] = [
    cascade_variant("PullbackCall_DeepWashout", pullback_call,
        "strategy_PullbackCall_DeepWashout_signal",
        "[RESEARCH] CALL after a deep quiet-volume washout."),
    cascade_variant("DeclineContinuationPut_ATR", decline_continuation_put,
        "strategy_DeclineContinuationPut_ATR_signal",
        "[RESEARCH] PUT after an ATR-scaled decline."),
    cascade_variant("BreakdownPut_20d", breakdown_put,
        "strategy_BreakdownPut_20d_signal",
        "[RESEARCH] PUT on a confirmed 20-session breakdown."),
    cascade_variant("RsiReversion_6040", rsi_reversion,
        "strategy_RsiReversion_6040_signal",
        "[RESEARCH] Two-sided RSI mean reversion."),
    cascade_variant("DRIFT_PROBE_PUT", drift_probe_put,
        "strategy_DRIFT_PROBE_PUT_signal",
        "[RESEARCH] PUT on negative opening drift."),
    cascade_variant("BollingerMeanReversion", bollinger_mean_reversion,
        "strategy_BollingerMeanReversion_signal",
        "[RESEARCH] Two-sided Bollinger band mean reversion."),
    cascade_variant("MacroDirection", macro_direction,
        "strategy_MacroDirection_signal",
        "[RESEARCH] Two-sided macro risk regime with hysteresis."),
]
PROMOTED_VARIANTS: list[StrategyVariant] = []
EXPERIMENTAL_VARIANTS: list[StrategyVariant] = [v for v in RESEARCH_VARIANTS if v.description.startswith("[RESEARCH]")]
DEFAULT_VARIANTS: list[StrategyVariant] = RESEARCH_VARIANTS


INTENT_THRESHOLDS_PATH = project_root / "src" / "technical_analysis" / "cascade" / "strategy_intent_thresholds.yaml"
STRATEGY_INTENT_DIAGNOSTIC_COLUMNS = [
    "strategy_variant",
    "signal_date",
    "direction",
    "actual_trade_label",
    "swing_structure_state",
    "next_day_swing_structure_state",
    "next_return_pct",
    "up_move_3d_pct",
    "down_move_3d_pct",
    "should_fire_intent",
    "did_fire",
    "is_hit",
    "match_type",
]
UPTREND_STATES = {"uptrend_intact", "structure_broken_up"}
DOWNTREND_STATES = {"downtrend_intact", "structure_broken_down"}

DEFAULT_STRATEGY_INTENT_THRESHOLDS: dict[str, dict[str, float | bool]] = {
    "ReversalCall": {
        "next_day_return_min": 0.001,
        "up_move_3d_min": 0.004,
        "support_distance_max": 0.75,
        "require_near_support": True,
    },
    "TrendPullbackCall": {
        "next_day_return_min": 0.0005,
        "up_move_3d_min": 0.006,
        "max_next_day_reversal": 0.001,
        "require_signal_uptrend": True,
        "require_next_day_uptrend": True,
    },
    "DowntrendBounceCall": {
        "next_day_return_min": 0.0005,
        "up_move_3d_min": 0.004,
        "require_signal_downtrend": True,
        "require_next_day_downtrend": True,
    },
    "DowntrendContinuationPut": {
        "next_day_return_max": -0.0005,
        "down_move_3d_min": 0.006,
        "min_next_day_bounce": 0.001,
        "require_signal_downtrend": True,
        "require_next_day_downtrend": True,
    },
    "UptrendExhaustionPut": {
        "next_day_return_max": -0.0005,
        "down_move_3d_min": 0.004,
        "min_next_day_bounce": 0.001,
        "require_signal_uptrend": True,
        "require_next_day_uptrend": True,
    },
    "ReversalPut": {
        "next_day_return_max": -0.001,
        "down_move_3d_min": 0.004,
        "resistance_distance_max": 0.02,
        "require_near_resistance": True,
    },
}


def strategy_definition_rows(
    variants: list[StrategyVariant] | None = None,
    include_all_registry: bool = False,
) -> list[dict]:
    registry = get_strategy_registry()
    if include_all_registry or variants is None:
        variant_names = list(registry.variants.keys())
    else:
        variant_names = [variant.name for variant in variants]

    rows = []
    for variant_name in variant_names:
        meta = registry.get_meta(variant_name)
        rows.append({
            "strategy_variant": variant_name,
            "strategy_type": meta.strategy_type,
            "direction": meta.direction,
            "description": meta.definition,
        })
    return rows


def _deep_merge_dict(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge_dict(base[key], value)
        else:
            base[key] = value
    return base


def load_strategy_intent_thresholds(path: Path = INTENT_THRESHOLDS_PATH) -> dict[str, dict[str, float | bool]]:
    out = copy.deepcopy(DEFAULT_STRATEGY_INTENT_THRESHOLDS)
    if not path.exists():
        return out
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return out
    payload = raw.get("strategy_intent_thresholds", raw.get("call_intent_thresholds", raw))
    if not isinstance(payload, dict):
        return out
    _deep_merge_dict(out, payload)
    return out


def load_call_intent_thresholds(path: Path = INTENT_THRESHOLDS_PATH) -> dict[str, dict[str, float | bool]]:
    """Backward-compatible alias used by older tests/imports."""
    return load_strategy_intent_thresholds(path)


def _numeric(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def _up_move_3d_pct(df: pd.DataFrame) -> pd.Series:
    entry = _numeric(df, "next_open")
    if "future_high_nd" in df.columns:
        high = _numeric(df, "future_high_nd")
    elif "future_high_3d" in df.columns:
        high = _numeric(df, "future_high_3d")
    else:
        high = _numeric(df, "next_high")
    return (high - entry) / entry.replace(0, np.nan)


def _down_move_3d_pct(df: pd.DataFrame) -> pd.Series:
    entry = _numeric(df, "next_open")
    if "future_low_nd" in df.columns:
        low = _numeric(df, "future_low_nd")
    elif "future_low_3d" in df.columns:
        low = _numeric(df, "future_low_3d")
    else:
        low = _numeric(df, "next_low")
    return (entry - low) / entry.replace(0, np.nan)


def _strategy_intent_key(strategy: str) -> str | None:
    if strategy == "ReversalCall":
        return "ReversalCall"
    if strategy == "TrendPullbackCall":
        return "TrendPullbackCall"
    if strategy == "DowntrendBounceCall":
        return "DowntrendBounceCall"
    if strategy == "DowntrendContinuationPut":
        return "DowntrendContinuationPut"
    if strategy == "UptrendExhaustionPut":
        return "UptrendExhaustionPut"
    if strategy == "ReversalPut":
        return "ReversalPut"
    return None


def _intent_direction(strategy_key: str | None) -> str | None:
    if strategy_key in {"ReversalCall", "TrendPullbackCall", "DowntrendBounceCall"}:
        return CALL
    if strategy_key in {"DowntrendContinuationPut", "UptrendExhaustionPut", "ReversalPut"}:
        return PUT
    return None


def _strategy_intent_mask(strategy: str, eligible: pd.DataFrame, thresholds: dict[str, dict[str, float | bool]]) -> pd.Series:
    strategy_key = _strategy_intent_key(strategy)
    if not strategy_key:
        return pd.Series(False, index=eligible.index)

    cfg = thresholds.get(strategy_key, {})
    labels = eligible["actual_trade_label"].fillna("").astype(str) if "actual_trade_label" in eligible.columns else pd.Series("", index=eligible.index)
    direction = _intent_direction(strategy_key)
    if direction == CALL:
        direction_opportunity = labels.isin([CALL, "BOTH"])
    elif direction == PUT:
        direction_opportunity = labels.isin([PUT, "BOTH"])
    else:
        direction_opportunity = pd.Series(False, index=eligible.index)
    next_ret = _numeric(eligible, "next_return_pct")
    up3 = _up_move_3d_pct(eligible)
    down3 = _down_move_3d_pct(eligible)
    state = eligible["swing_structure_state"].fillna("").astype(str).str.lower() if "swing_structure_state" in eligible.columns else pd.Series("", index=eligible.index)
    next_state = state.shift(-1)

    if strategy_key == "ReversalCall":
        support_dist = _numeric(eligible, "support_distance_10d")
        near_validated_support = (
            eligible["near_validated_support_10d"].fillna(False).astype(bool)
            if "near_validated_support_10d" in eligible.columns
            else pd.Series(False, index=eligible.index)
        )
        max_support = float(cfg.get("support_distance_max", 0.75))
        near_support = near_validated_support | ((support_dist >= 0) & (support_dist <= max_support))
        support_ok = near_support if bool(cfg.get("require_near_support", True)) else pd.Series(True, index=eligible.index)
        return (
            direction_opportunity
            & (next_ret >= float(cfg.get("next_day_return_min", 0.001)))
            & (up3 >= float(cfg.get("up_move_3d_min", 0.004)))
            & support_ok
        ).fillna(False)

    if strategy_key == "TrendPullbackCall":
        signal_uptrend = state.isin(UPTREND_STATES)
        next_uptrend = next_state.isin(UPTREND_STATES)
        signal_ok = signal_uptrend if bool(cfg.get("require_signal_uptrend", True)) else pd.Series(True, index=eligible.index)
        next_ok = next_uptrend if bool(cfg.get("require_next_day_uptrend", True)) else pd.Series(True, index=eligible.index)
        return (
            direction_opportunity
            & (next_ret >= float(cfg.get("next_day_return_min", 0.0005)))
            & (next_ret >= -abs(float(cfg.get("max_next_day_reversal", 0.001))))
            & (up3 >= float(cfg.get("up_move_3d_min", 0.006)))
            & signal_ok
            & next_ok
        ).fillna(False)

    if strategy_key == "DowntrendBounceCall":
        signal_downtrend = state.isin(DOWNTREND_STATES)
        next_downtrend = next_state.isin(DOWNTREND_STATES)
        signal_ok = signal_downtrend if bool(cfg.get("require_signal_downtrend", True)) else pd.Series(True, index=eligible.index)
        next_ok = next_downtrend if bool(cfg.get("require_next_day_downtrend", True)) else pd.Series(True, index=eligible.index)
        return (
            direction_opportunity
            & (next_ret >= float(cfg.get("next_day_return_min", 0.0005)))
            & (up3 >= float(cfg.get("up_move_3d_min", 0.004)))
            & signal_ok
            & next_ok
        ).fillna(False)

    if strategy_key == "DowntrendContinuationPut":
        signal_downtrend = state.isin(DOWNTREND_STATES)
        next_downtrend = next_state.isin(DOWNTREND_STATES)
        signal_ok = signal_downtrend if bool(cfg.get("require_signal_downtrend", True)) else pd.Series(True, index=eligible.index)
        next_ok = next_downtrend if bool(cfg.get("require_next_day_downtrend", True)) else pd.Series(True, index=eligible.index)
        return (
            direction_opportunity
            & (next_ret <= float(cfg.get("next_day_return_max", -0.0005)))
            & (next_ret <= abs(float(cfg.get("min_next_day_bounce", 0.001))))
            & (down3 >= float(cfg.get("down_move_3d_min", 0.006)))
            & signal_ok
            & next_ok
        ).fillna(False)

    if strategy_key == "UptrendExhaustionPut":
        signal_uptrend = state.isin(UPTREND_STATES)
        next_uptrend = next_state.isin(UPTREND_STATES)
        signal_ok = signal_uptrend if bool(cfg.get("require_signal_uptrend", True)) else pd.Series(True, index=eligible.index)
        next_ok = next_uptrend if bool(cfg.get("require_next_day_uptrend", True)) else pd.Series(True, index=eligible.index)
        return (
            direction_opportunity
            & (next_ret <= float(cfg.get("next_day_return_max", -0.0005)))
            & (next_ret <= abs(float(cfg.get("min_next_day_bounce", 0.001))))
            & (down3 >= float(cfg.get("down_move_3d_min", 0.004)))
            & signal_ok
            & next_ok
        ).fillna(False)

    resistance_dist = _numeric(eligible, "resistance_distance_10d")
    near_validated_resistance = (
        eligible["near_validated_resistance_10d"].fillna(False).astype(bool)
        if "near_validated_resistance_10d" in eligible.columns
        else pd.Series(False, index=eligible.index)
    )
    near_resistance = near_validated_resistance | (resistance_dist <= float(cfg.get("resistance_distance_max", 0.02)))
    resistance_ok = near_resistance if bool(cfg.get("require_near_resistance", True)) else pd.Series(True, index=eligible.index)
    return (
        direction_opportunity
        & (next_ret <= float(cfg.get("next_day_return_max", -0.001)))
        & (down3 >= float(cfg.get("down_move_3d_min", 0.004)))
        & resistance_ok
    ).fillna(False)


def _call_intent_mask(strategy: str, eligible: pd.DataFrame, thresholds: dict[str, dict[str, float | bool]]) -> pd.Series:
    """Backward-compatible alias used by external scripts/imports."""
    return _strategy_intent_mask(strategy, eligible, thresholds)


def call_intent_metrics(
    strategy: str,
    eligible: pd.DataFrame | None,
    eligible_signal: pd.Series | None,
    thresholds: dict[str, dict[str, float | bool]],
) -> tuple[dict[str, float | int | None], pd.DataFrame]:
    empty_metrics = {
        "call_or_both_days": None,
        "intent_coverage_pct": None,
        "intent_fires": None,
        "intent_hits": None,
        "intent_F1": None,
        "intent_missed_opportunities": None,
        "intent_extra_fires": None,
    }
    if eligible is None or eligible_signal is None or eligible.empty:
        return empty_metrics, pd.DataFrame(columns=STRATEGY_INTENT_DIAGNOSTIC_COLUMNS)

    strategy_key = _strategy_intent_key(strategy)
    direction = _intent_direction(strategy_key)
    if not strategy_key or direction is None:
        return empty_metrics, pd.DataFrame(columns=STRATEGY_INTENT_DIAGNOSTIC_COLUMNS)

    should_fire = _strategy_intent_mask(strategy, eligible, thresholds)
    did_fire = eligible_signal.reset_index(drop=True).isin([direction])
    hits = should_fire & did_fire
    labels = eligible["actual_trade_label"].fillna("").astype(str) if "actual_trade_label" in eligible.columns else pd.Series("", index=eligible.index)
    call_or_both_days = int(labels.isin([direction, "BOTH"]).sum())

    opportunities = int(should_fire.sum())
    fires = int(did_fire.sum())
    hit_count = int(hits.sum())
    coverage = (opportunities / call_or_both_days * 100.0) if call_or_both_days else None
    precision = (hit_count / fires) if fires else None
    recall = (hit_count / opportunities) if opportunities else None
    f1 = (2 * precision * recall / (precision + recall)) if (precision is not None and recall is not None and (precision + recall) > 0) else None
    missed = int((should_fire & ~did_fire).sum())
    extra = int((did_fire & ~should_fire).sum())

    next_ret = _numeric(eligible, "next_return_pct")
    up3 = _up_move_3d_pct(eligible)
    down3 = _down_move_3d_pct(eligible)
    state = eligible["swing_structure_state"].fillna("").astype(str) if "swing_structure_state" in eligible.columns else pd.Series("", index=eligible.index)
    next_state = state.shift(-1)
    signal_dates = eligible["signal_date"] if "signal_date" in eligible.columns else pd.Series([None] * len(eligible))
    match_type = np.select(
        [hits, should_fire & ~did_fire, ~should_fire & did_fire],
        ["hit", "missed_intent", "extra_fire"],
        default="neutral",
    )
    diag = pd.DataFrame({
        "strategy_variant": strategy,
        "signal_date": signal_dates,
        "direction": direction,
        "actual_trade_label": labels,
        "swing_structure_state": state,
        "next_day_swing_structure_state": next_state,
        "next_return_pct": next_ret,
        "up_move_3d_pct": up3,
        "down_move_3d_pct": down3,
        "should_fire_intent": should_fire,
        "did_fire": did_fire,
        "is_hit": hits,
        "match_type": match_type,
    })

    return {
        "call_or_both_days": call_or_both_days,
        "intent_coverage_pct": coverage,
        "intent_fires": fires,
        "intent_hits": hit_count,
        "intent_F1": f1,
        "intent_missed_opportunities": missed,
        "intent_extra_fires": extra,
    }, diag


def export_strategy_definitions(
    output_dir: Path,
    variants: list[StrategyVariant] | None = None,
    include_all_registry: bool = True,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "strategy_grid_definitions.csv"
    pd.DataFrame(
        strategy_definition_rows(
            variants=variants,
            include_all_registry=include_all_registry,
        )
    ).to_csv(path, index=False)
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
        price   Ã¢â‚¬â€ option price at each snapshot (ffill filled)
        entries Ã¢â‚¬â€ True at the entry snapshot
        exits   Ã¢â‚¬â€ True at the exit snapshot
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
    from src.technical_analysis.prediction.signal_strength import add_raw_direction

    base = add_raw_direction(build_base())
    base["signal_date_dt"] = pd.to_datetime(base["signal_date"]).dt.date
    base = base.reset_index(drop=True)

    eligible_mask = pd.Series(True, index=base.index)
    if start:
        eligible_mask &= base["signal_date_dt"] >= start
    if end:
        eligible_mask &= base["signal_date_dt"] <= end

    all_predictions: list[pd.DataFrame] = []
    strategy_intent_diagnostics: list[pd.DataFrame] = []
    leaderboard: list[dict] = []
    definitions: list[dict] = strategy_definition_rows(variants=variants, include_all_registry=False)
    intent_thresholds = load_strategy_intent_thresholds()

    for variant in variants:
        signal = variant.signal_fn(base)
        eligible = base.loc[eligible_mask].copy().reset_index(drop=True)
        eligible_signal = signal.loc[eligible_mask].reset_index(drop=True)
        intent_metrics, intent_diag = call_intent_metrics(variant.name, eligible, eligible_signal, intent_thresholds)
        if not intent_diag.empty:
            strategy_intent_diagnostics.append(intent_diag)
        predictions = research_prediction_rows(variant, eligible, eligible_signal)
        if not predictions.empty:
            all_predictions.append(predictions)
        target_value = float(RESEARCH_TARGET_PCT_BY_STRATEGY.get(variant.name, 0.006))
        stop_value: float | str | None
        if variant.name in RESEARCH_ATR_SCALED_STOP_STRATEGIES:
            stop_value = "ATR_0.4%-0.8%"
        else:
            stop_value = float(RESEARCH_FIXED_STOP_PCT_BY_STRATEGY.get(variant.name, 0.004))
        meta = get_strategy_registry().get_meta(variant.name)
        sides = (CALL, PUT) if meta.direction == "TWO_SIDED" else (meta.direction,)
        for side in sides:
            leaderboard.append(leaderboard_row(
                variant.name, {}, pd.DataFrame(), pd.DataFrame(), target_value, stop_value,
                eligible=eligible,
                eligible_signal=eligible_signal,
                intent_metrics=intent_metrics,
                side=side,
            ))
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "leaderboard": output_dir / "strategy_grid_leaderboard.csv",
        "predictions": output_dir / "strategy_grid_predictions.csv",
        "definitions": output_dir / "strategy_grid_definitions.csv",
        "intent_diagnostics_call": output_dir / "strategy_grid_intent_diagnostics_call.csv",
        "summary": output_dir / "strategy_grid_summary.txt",
    }
    pd.DataFrame(leaderboard).sort_values(
        ["actualTradeLabel_F1", "actualTradeLabel_precision", "actualTradeLabel_recall", "fires"],
        ascending=False,
        na_position="last",
    ).to_csv(paths["leaderboard"], index=False)
    (
        pd.concat(all_predictions, ignore_index=True)
        if all_predictions else pd.DataFrame(columns=RESEARCH_PREDICTION_COLUMNS)
    ).to_csv(paths["predictions"], index=False)
    pd.DataFrame(definitions).to_csv(paths["definitions"], index=False)
    (
        pd.concat(strategy_intent_diagnostics, ignore_index=True)
        if strategy_intent_diagnostics else pd.DataFrame(columns=STRATEGY_INTENT_DIAGNOSTIC_COLUMNS)
    ).to_csv(paths["intent_diagnostics_call"], index=False)
    write_summary(paths["summary"], leaderboard, definitions)
    return paths


def research_prediction_rows(
    variant: StrategyVariant,
    eligible: pd.DataFrame,
    eligible_signal: pd.Series,
) -> pd.DataFrame:
    """One row per strategy variant x signal date where the variant fired."""
    if eligible.empty:
        return pd.DataFrame(columns=RESEARCH_PREDICTION_COLUMNS)

    sig = eligible_signal.reset_index(drop=True)
    elig = eligible.reset_index(drop=True)
    fired = sig.isin([CALL, PUT])
    if not fired.any():
        return pd.DataFrame(columns=RESEARCH_PREDICTION_COLUMNS)

    meta = get_strategy_registry().get_meta(variant.name)
    fired_rows = elig.loc[fired].copy()
    def _fired_numeric(column: str) -> pd.Series:
        values = (
            fired_rows[column]
            if column in fired_rows.columns
            else pd.Series(np.nan, index=fired_rows.index)
        )
        return pd.to_numeric(values, errors="coerce")

    next_open = _fired_numeric("next_open")
    next_high = _fired_numeric("next_high")
    next_low = _fired_numeric("next_low")
    out = pd.DataFrame({
        "strategy_variant": variant.name,
        "strategy_type": meta.strategy_type,
        "signal_date": fired_rows["signal_date"].values,
        "trade_date": fired_rows["next_trade_date"].values if "next_trade_date" in fired_rows.columns else None,
        "predicted": sig.loc[fired].values,
        "actual_label": fired_rows["actual_trade_label"].values if "actual_trade_label" in fired_rows.columns else None,
        "quality_label": fired_rows["actual_quality_label"].values if "actual_quality_label" in fired_rows.columns else None,
        "max_underlying_up": ((next_high - next_open) / next_open).values,
        "max_underlying_down": ((next_open - next_low) / next_open).values,
        "us_ret": fired_rows["global_us_return_mean"].values if "global_us_return_mean" in fired_rows.columns else None,
        "europe_ret": fired_rows["global_europe_return_mean"].values if "global_europe_return_mean" in fired_rows.columns else None,
        "asia_ret": fired_rows["global_asia_return_mean"].values if "global_asia_return_mean" in fired_rows.columns else None,
    })
    return out.reindex(columns=RESEARCH_PREDICTION_COLUMNS)


def build_atm_option_trade_plans(
    df: pd.DataFrame,
    signal: pd.Series,
    strategy_name: str,
) -> pd.DataFrame:
    rows: list[dict] = []
    is_atr_scaled = strategy_name in RESEARCH_ATR_SCALED_STOP_STRATEGIES
    default_target = float(RESEARCH_TARGET_PCT_BY_STRATEGY.get(strategy_name, 0.006))
    default_stop = RESEARCH_FIXED_STOP_PCT_BY_STRATEGY.get(strategy_name, 0.004)

    def _atr_scaled_stop(row: pd.Series) -> float:
        close_px = _float_or_none(row.get("close_1515"))
        atr14 = _float_or_none(row.get("atr14"))
        if close_px in (None, 0) or atr14 is None:
            return 0.006
        raw = 0.5 * (float(atr14) / float(close_px))
        return max(0.004, min(0.008, raw))

    for idx, row in df.iterrows():
        side = str(signal.iloc[idx])
        if side not in {CALL, PUT}:
            continue
        next_trade_date = _date_or_none(row.get("next_trade_date"))
        if next_trade_date is None:
            continue
        option_type = "CE" if side == CALL else "PE"
        target_pct = default_target
        stop_loss_pct = _atr_scaled_stop(row) if is_atr_scaled else float(default_stop)
        target_label = f"t{target_pct:g}".replace(".", "p")
        stop_label = f"sl{stop_loss_pct:g}".replace(".", "p")
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
    from src.execution.cascade import compute_delta_adjusted_levels

    selected["primary_buy_entry_price"] = selected["entry_price"]
    levels = [
        compute_delta_adjusted_levels(
            entry_price=float(row.entry_price),
            underlying_price=float(row.entry_underlying_price),
            option_delta=row.entry_option_delta,
            target_pct=float(row.target_pct),
            stop_loss_pct=float(row.stop_loss_pct),
        )
        for row in selected.itertuples(index=False)
    ]
    selected["target_1_price"] = [level.target_price for level in levels]
    selected["target_2_price"] = selected["target_1_price"]
    selected["stop_loss_enabled"] = pd.to_numeric(selected["stop_loss_pct"], errors="coerce").fillna(0) > 0
    selected["stop_loss_price"] = [level.stop_loss_price for level in levels]
    selected["entry_option_delta"] = [level.option_delta for level in levels]
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
                        os.last_price,
                        calc.delta,
                        uc.close_price
                    FROM "OptionSnapshot" os
                    JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
                    JOIN "OptionSnapshotCalc" calc ON calc.option_snapshot_id = os.id
                    JOIN "UnderlyingCandle5m" uc
                      ON UPPER(uc.underlying) = 'NIFTY'
                     AND uc.trade_date = os.trade_date
                     AND uc.candle_time::time = '09:15:00'
                    WHERE UPPER(oi.underlying) = 'NIFTY'
                      AND oi.instrument_type = %s
                      AND os.trade_date = %s
                      AND os.snapshot_label = 'M5_0915'
                      AND os.last_price IS NOT NULL
                      AND os.last_price > 0
                      AND calc.delta IS NOT NULL
                      AND ABS(calc.delta) BETWEEN 0.50 AND 0.70
                      AND (oi.expiry - os.trade_date) BETWEEN 14 AND 28
                      AND (
                          (oi.instrument_type = 'CE' AND oi.strike <= uc.close_price
                           AND oi.strike >= uc.close_price * 0.98)
                          OR
                          (oi.instrument_type = 'PE' AND oi.strike >= uc.close_price
                           AND oi.strike <= uc.close_price * 1.02)
                      )
                    ORDER BY ABS((oi.expiry - os.trade_date) - 21),
                             ABS(ABS(calc.delta) - 0.58),
                             os.snapshot_time DESC
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
                    "entry_option_delta": float(row[7]) if row[7] is not None else None,
                    "entry_underlying_price": float(row[8]),
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
    """Load intraday option snapshots for the execution session only."""
    if plans.empty:
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "trade_date", "price", "lot_size"])

    n_hold_days = 1

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
    # Normalise vectorbt column names Ã¢â€ â€™ internal names used by leaderboard_row.
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
    stop_loss_pct: float | str | None,
    eligible: pd.DataFrame | None = None,
    eligible_signal: pd.Series | None = None,
    intent_metrics: dict[str, float | int | None] | None = None,
    side: str | None = None,
) -> dict:
    strategy_meta = get_strategy_registry().get_meta(strategy)
    strategy_direction = side or strategy_meta.direction
    evaluation_signal = eligible_signal
    if side is not None and eligible_signal is not None:
        evaluation_signal = eligible_signal.where(eligible_signal == side, FLAT)
    # Direction win rate: did NIFTY touch the target_pct threshold from next_open?
    # Uses the common target threshold matching production labelling.
    direction_wins = None
    direction_win_rate = None
    opportunities = None
    if eligible is not None and evaluation_signal is not None and not eligible.empty:
        sig = evaluation_signal.reset_index(drop=True)
        base_r = eligible.reset_index(drop=True)
        labels = (
            base_r["actual_trade_label"].fillna("").astype(str)
            if "actual_trade_label" in base_r.columns
            else pd.Series("", index=base_r.index)
        )
        if strategy_direction == CALL:
            opportunities = int(labels.isin([CALL, "BOTH"]).sum())
        elif strategy_direction == PUT:
            opportunities = int(labels.isin([PUT, "BOTH"]).sum())
        else:
            opportunities = int(labels.isin([CALL, PUT, "BOTH"]).sum())
        fired = sig.isin([CALL, PUT])
        if fired.any():
            fired_rows = base_r.loc[fired].copy()
            fired_sig = sig.loc[fired].reset_index(drop=True)
            fired_rows = fired_rows.reset_index(drop=True)
            o = pd.to_numeric(fired_rows["next_open"], errors="coerce")
            h = pd.to_numeric(fired_rows["next_high"], errors="coerce")
            lo = pd.to_numeric(fired_rows["next_low"], errors="coerce")
            call_hit = (h - o) / o >= TARGET_THRESHOLD
            put_hit = (o - lo) / o >= TARGET_THRESHOLD
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

    if eligible is not None and evaluation_signal is not None and "raw_signal_quality" in eligible.columns:
        sig = evaluation_signal.reset_index(drop=True)
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
    if eligible is not None and evaluation_signal is not None and "actual_quality_label" in eligible.columns:
        from src.technical_analysis.prediction.signal_strength import quality_label_metrics
        quality_label_result = quality_label_metrics(
            evaluation_signal.reset_index(drop=True),
            eligible.reset_index(drop=True)["actual_quality_label"],
            side=side,
        )

    actual_trade_label_result = {
        "actualTradeLabel_precision": None,
        "actualTradeLabel_recall": None,
        "actualTradeLabel_F1": None,
    }
    if eligible is not None and evaluation_signal is not None:
        from src.technical_analysis.cascade.engine import score_signal
        scored = score_signal(
            eligible.reset_index(drop=True),
            evaluation_signal.reset_index(drop=True),
            strategy,
        )
        actual_trade_label_result = {
            "actualTradeLabel_precision": scored.precision if scored.precision == scored.precision else None,
            "actualTradeLabel_recall": scored.recall if scored.recall == scored.recall else None,
            "actualTradeLabel_F1": scored.f1 if scored.f1 == scored.f1 else None,
        }

    return {
        "strategy_variant": strategy,
        "strategy_type": strategy_meta.strategy_type,
        "side": side or strategy_meta.direction,
        "fires": call_fires + put_fires,
        "opportunities": opportunities,
        "direction_wins": direction_wins,
        "direction_win_rate_pct": direction_win_rate,
        "call_or_both_days": (intent_metrics or {}).get("call_or_both_days"),
        "intent_coverage_pct": (intent_metrics or {}).get("intent_coverage_pct"),
        "intent_fires": (intent_metrics or {}).get("intent_fires"),
        "intent_hits": (intent_metrics or {}).get("intent_hits"),
        "intent_F1": (intent_metrics or {}).get("intent_F1"),
        "intent_missed_opportunities": (intent_metrics or {}).get("intent_missed_opportunities"),
        "intent_extra_fires": (intent_metrics or {}).get("intent_extra_fires"),
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
    }


def write_summary(path: Path, leaderboard: list[dict], definitions: list[dict]) -> None:
    ranked = sorted(
        leaderboard,
        key=lambda row: (
            row.get("actualTradeLabel_F1") or 0,
            row.get("actualTradeLabel_precision") or 0,
            row.get("actualTradeLabel_recall") or 0,
        ),
        reverse=True,
    )
    lines = ["NIFTY prediction strategy grid summary", "", "Leaderboard:"]
    for row in ranked:
        lines.append(
            f"- {row['strategy_variant']} ({row.get('side')}): fires={row['fires']} "
            f"precision={row.get('actualTradeLabel_precision')} "
            f"recall={row.get('actualTradeLabel_recall')} "
            f"F1={row.get('actualTradeLabel_F1')}"
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
    parser.add_argument("--start", default=None, help="Start signal date YYYY-MM-DD; omit for full history")
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
        help="Refresh strategy_grid_definitions.csv without running prediction evaluation.",
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
            Path(args.output_dir), selected_variants or DEFAULT_VARIANTS, include_all_registry=False
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
