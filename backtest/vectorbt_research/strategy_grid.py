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
    calm_momentum_put,
    calm_trend_call,
    down_momentum_put,
    ma_alignment_room,
    mean_reversion,
    momentum_directional,
    oversold_bounce_call,
    range_breakout,
)

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
    """
    def signal(df: pd.DataFrame) -> pd.Series:
        return family_fn(df)[signal_key]
    return StrategyVariant(name=name, signal_fn=signal, description=description or signal_key)


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


# â”€â”€ promoted cascade variants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# These mirror the signals that cleared the precision floor in the research
# harness (build_experiment.py). Use cascade_variant() to pull one named signal
# out of any cascade family function.

def _momentum_directional_call_guard(df: pd.DataFrame) -> pd.Series:
    """MomentumDirectional base CALL + bb_width >= 5.5% (the production signal)."""
    base = momentum_directional(df)["strategy_MomentumDirectional_signal"]
    return _sig((base == CALL) & (df["bb_width"] >= 0.055), CALL)


def _momentum_directional_two_sided(df: pd.DataFrame) -> pd.Series:
    """MomentumDirectional base two-sided (CALL >=2 votes / PUT >=3 votes)."""
    return momentum_directional(df)["strategy_MomentumDirectional_signal"]


PROMOTED_VARIANTS: list[StrategyVariant] = [
    # Production CALL: MomentumDirectional base + BB expansion guard
    StrategyVariant(
        name="MomentumDirectional_CallExpGuard",
        signal_fn=_momentum_directional_call_guard,
        description="CALL when MomentumDirectional (>=2 votes) fires CALL and bb_width >= 5.5%. Mirrors the production signal.",
    ),
    # Base MomentumDirectional two-sided (call/put)
    StrategyVariant(
        name="MomentumDirectional",
        signal_fn=_momentum_directional_two_sided,
        description="Two-sided: CALL on >=2 oversold votes, PUT on >=3 down-momentum votes; conflict resolved by normalised strength.",
    ),
    # OversoldBounce ContextRoom (promoted CALL, stress regime)
    cascade_variant(
        "OversoldBounceCall_ContextRoom",
        oversold_bounce_call,
        "strategy_OversoldBounceCall_ContextRoom_signal",
        "CALL when rsi14 <= dynamic rolling cap, resistance room clears dynamic floor, vix >= 12.",
    ),
    # DownMomentumPut MoreTrades (promoted PUT, stress regime)
    cascade_variant(
        "DownMomentumPut_MoreTrades",
        down_momentum_put,
        "strategy_DownMomentumPut_MoreTrades_signal",
        "PUT when ma20_slope <= -0.3%, adaptive volume floor cleared, vix >= 12.",
    ),
    # RSI mean-reversion oversold/overbought levels (two-sided, stress+calm)
    cascade_variant(
        "RsiMeanReversion_6040",
        mean_reversion,
        "strategy_RsiMeanReversion_6040_signal",
        "CALL when rsi14 < 40; PUT when rsi14 > 60. Ungated.",
    ),
    # Bollinger mean-reversion: price touches outside the band (two-sided)
    cascade_variant(
        "BollingerMeanReversion",
        mean_reversion,
        "strategy_BollingerMeanReversion_signal",
        "CALL when close < lower Bollinger band (20d mean - 2sigma); PUT when > upper band.",
    ),
]

# â”€â”€ experimental cascade variants (not in production) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# These are strategies that exist in cascade/strategies.py but have NOT yet
# cleared the production precision floor. They are stress-regime or calm-regime
# specific and are gated to their appropriate regime via _gate_to_regime().

EXPERIMENTAL_VARIANTS: list[StrategyVariant] = [
    # --- stress regime: additional OversoldBounce variants ---
    cascade_variant(
        "OversoldBounceCall_HighPrecision",
        oversold_bounce_call,
        "strategy_OversoldBounceCall_HighPrecision_signal",
        "CALL when range_position_10d <= 20th pctile and VIX >= 12 (stress, highest precision).",
    ),
    cascade_variant(
        "OversoldBounceCall_MoreTrades",
        oversold_bounce_call,
        "strategy_OversoldBounceCall_MoreTrades_signal",
        "CALL when rsi14 <= 42, resistance room >= 2.5%, VIX >= 12 (stress, more trades).",
    ),
    # --- stress regime: additional DownMomentumPut variants ---
    cascade_variant(
        "DownMomentumPut_HighPrecision",
        down_momentum_put,
        "strategy_DownMomentumPut_HighPrecision_signal",
        "PUT when ma20_slope <= -0.3%, volume floor, delta VIX positive (stress, high precision).",
    ),
    cascade_variant(
        "DownMomentumPut_Fast",
        down_momentum_put,
        "strategy_DownMomentumPut_Fast_signal",
        "PUT when ma5_slope <= -0.2% and ret_3d <= -0.5% (stress, faster entry).",
    ),
    # --- stress regime: MomentumDirectional variants ---
    cascade_variant(
        "MomentumDirectional_ContextVotes_ExpansionGuard",
        momentum_directional,
        "strategy_MomentumDirectional_ContextVotes_ExpansionGuard_signal",
        "Two-sided with bb_width >= 5.5% and resistance room >= 1.5% guard.",
    ),
    cascade_variant(
        "MomentumDirectional_ContextVotes_StrongExpansionGuard",
        momentum_directional,
        "strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal",
        "Two-sided with VIX >= 16 and bb_width >= 6.5% guard (tightest).",
    ),
    # --- experimental: MA alignment (stress, not yet in production) ---
    cascade_variant(
        "MAAlignmentRoom_ReboundCall",
        ma_alignment_room,
        "strategy_MAAlignmentRoom_ReboundCall_signal",
        "CALL on MA5/MA10/MA20 upward alignment + resistance room.",
    ),
    cascade_variant(
        "MAAlignmentRoom_PutGuarded",
        ma_alignment_room,
        "strategy_MAAlignmentRoom_PutGuarded_signal",
        "PUT when MA stack inverted and range_position < 50% and ret_5d < 0.",
    ),
    cascade_variant(
        "MaTrend_001",
        ma_alignment_room,
        "strategy_MaTrend_001_signal",
        "Two-sided: CALL when MA10/MA20 spread > 0.1%, PUT when < -0.1%.",
    ),
    # --- experimental: range breakout (stress, not yet in production) ---
    cascade_variant(
        "RangeBreakout",
        range_breakout,
        "strategy_RangeBreakout_signal",
        "CALL when close breaks prior session high; PUT when below prior low.",
    ),
    cascade_variant(
        "RangeBreakout_ATRBuffer",
        range_breakout,
        "strategy_RangeBreakout_ATRBuffer_signal",
        "Same as RangeBreakout but entry is inside the range by 0.15*ATR (earlier signal).",
    ),
    # --- calm regime: trend following ---
    StrategyVariant(
        name="CalmTrendCall_ContextHeadroom",
        signal_fn=_gate_to_regime(
            lambda df: calm_trend_call(df)["strategy_CalmTrendCall_ContextHeadroom_signal"],
            REGIME_CALM,
        ),
        description="CALL when MA20 uptrend + dynamic resistance room floor + te >= 25%. Calm regime only.",
    ),
    StrategyVariant(
        name="CalmTrendCall_Pullback",
        signal_fn=_gate_to_regime(
            lambda df: calm_trend_call(df)["strategy_CalmTrendCall_Pullback_signal"],
            REGIME_CALM,
        ),
        description="CALL when MA20 uptrend + range_position_10d <= 50% + trend_efficiency >= 25%. Calm regime only.",
    ),
    # --- calm regime: fade overbought ---
    StrategyVariant(
        name="CalmFadePut_ContextOverbought",
        signal_fn=_gate_to_regime(
            lambda df: calm_fade_put(df)["strategy_CalmFadePut_ContextOverbought_signal"],
            REGIME_CALM,
        ),
        description="PUT when rsi14 and rsi5 both exceed rolling overbought floor. Calm regime only.",
    ),
    # --- calm regime: momentum continuation ---
    StrategyVariant(
        name="CalmMomentumPut_Continuation",
        signal_fn=_gate_to_regime(
            lambda df: calm_momentum_put(df)["strategy_CalmMomentumPut_Continuation_signal"],
            REGIME_CALM,
        ),
        description="PUT when ret_3d <= -0.3% (price has been falling, momentum continues). Calm regime only.",
    ),
]

# â”€â”€ simple parametric variants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DEFAULT_VARIANTS: list[StrategyVariant] = [
    # 1. promoted production cascade signals
    *PROMOTED_VARIANTS,
    # 2. experimental cascade signals (not in production roster)
    *EXPERIMENTAL_VARIANTS,
    # 3. simple parametric grid variants for quick threshold sweeps
    ma_spread_variant("MaSpread_001_Rsi6040", 0.001, 60, 40),
    rsi_reversion_variant("RsiReversion_6040", 40, 60),
    room_alignment_variant("MAAlignmentRoom_Fast", 0.005, 0.000, 60, 40),
    macd_variant("MACD_EMA5_20", 5, 20),
    macd_variant("MACD_EMA10_30", 10, 30)
]


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
    base = _add_regime_column(build_base())
    base["trade_date_dt"] = pd.to_datetime(base["trade_date"]).dt.date
    base = base.reset_index(drop=True)

    eligible_mask = pd.Series(True, index=base.index)
    if start:
        eligible_mask &= base["trade_date_dt"] >= start
    if end:
        eligible_mask &= base["trade_date_dt"] <= end

    all_plans: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []
    leaderboard: list[dict] = []
    definitions: list[dict] = []

    for variant in variants:
        signal = variant.signal_fn(base)
        eligible = base.loc[eligible_mask].copy().reset_index(drop=True)
        eligible_signal = signal.loc[eligible_mask].reset_index(drop=True)
        for target_value in target_grid:
            for stop_value in stop_loss_grid:
                plans = build_atm_option_trade_plans(eligible, eligible_signal, variant.name, target_value, stop_value)
                snapshots = load_replay_snapshots(plans)
                trades = build_replay_trades(plans, snapshots, fees=fees, slippage=slippage)
                enriched = enrich_grid_trades(trades, plans, snapshots, used_vectorbt=False)
                if not enriched.empty:
                    enriched["strategy_variant"] = variant.name
                    all_trades.append(enriched)
                if not plans.empty:
                    all_plans.append(plans)
                leaderboard.append(leaderboard_row(variant.name, {}, enriched, plans, target_value, stop_value))
        definitions.append({"strategy_variant": variant.name, "description": variant.description})

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "leaderboard": output_dir / "strategy_grid_leaderboard.csv",
        "trades": output_dir / "strategy_grid_trades.csv",
        "plans": output_dir / "strategy_grid_trade_plans.csv",
        "definitions": output_dir / "strategy_grid_definitions.csv",
        "summary": output_dir / "strategy_grid_summary.txt",
    }
    pd.DataFrame(leaderboard).sort_values(["total_pnl_per_unit", "win_rate_pct"], ascending=False).to_csv(paths["leaderboard"], index=False)
    pd.concat(all_trades, ignore_index=True).to_csv(paths["trades"], index=False) if all_trades else pd.DataFrame().to_csv(paths["trades"], index=False)
    pd.concat(all_plans, ignore_index=True).to_csv(paths["plans"], index=False) if all_plans else pd.DataFrame().to_csv(paths["plans"], index=False)
    pd.DataFrame(definitions).to_csv(paths["definitions"], index=False)
    write_summary(paths["summary"], leaderboard, definitions)
    return paths


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
            "trade_id": f"{strategy_name}_{target_label}_{stop_label}_{row['trade_date']}_{option_type}",
            "strategy_variant": strategy_name,
            "trade_date": row["trade_date_dt"],
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
                    WHERE UPPER(oi.underlying) = 'NIFTY'
                      AND oi.instrument_type = %s
                      AND os.trade_date = %s
                      AND os.last_price IS NOT NULL
                      AND os.last_price > 0
                    ORDER BY ABS(oi.strike - %s), oi.expiry, os.snapshot_time
                    LIMIT 1
                    """,
                    (plan.option_type, plan.replay_trade_date, plan.spot_price),
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


def load_replay_snapshots(plans: pd.DataFrame) -> pd.DataFrame:
    if plans.empty:
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "trade_date", "price", "lot_size"])

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        frames: list[pd.DataFrame] = []
        with db.conn.cursor() as cur:
            for plan in plans.itertuples(index=False):
                cur.execute(
                    """
                    SELECT os.trade_date, os.snapshot_time, os.last_price AS price, oi.lot_size
                    FROM "OptionSnapshot" os
                    JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
                    WHERE oi.instrument_token = %s
                      AND os.trade_date = %s
                      AND os.last_price IS NOT NULL
                      AND os.last_price > 0
                    ORDER BY os.snapshot_time
                    """,
                    (int(plan.primary_buy_token), plan.replay_trade_date),
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
        "trade_id", "strategy_variant", "trade_date", "replay_trade_date",
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
) -> dict:
    pnl = pd.to_numeric(trades.get("pnl_per_unit", pd.Series(dtype=float)), errors="coerce").fillna(0)
    pnl_lot = pd.to_numeric(trades.get("pnl_per_lot", pd.Series(dtype=float)), errors="coerce").fillna(0)
    n = len(pnl)
    wins = int((pnl > 0).sum()) if not trades.empty else 0
    losses = int((pnl < 0).sum()) if not trades.empty else 0
    win_rate = round(wins / n * 100, 1) if n else None
    return {
        "strategy_variant": strategy,
        "target_pct": target_pct,
        "stop_loss_pct": stop_loss_pct,
        "plans": len(plans),
        "trades": n or int(metrics.get("trades", 0) or 0),
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
    parser.add_argument("--start", default=None, help="Start signal date YYYY-MM-DD")
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
    args = parser.parse_args()

    selected_variants: list[StrategyVariant] | None = None
    if args.variants:
        filters = [f.strip().lower() for f in args.variants.split(",") if f.strip()]
        selected_variants = [v for v in DEFAULT_VARIANTS if any(f in v.name.lower() for f in filters)]
        if not selected_variants:
            print(f"[WARN] --variants filter '{args.variants}' matched no variants; running all.")
            selected_variants = None

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

