"""
NIFTY production prediction pipeline.

This is the PRODUCTION counterpart to the research harness in
backtest/vectorbt_research/strategy_grid.py. The cascade engine (dataset assembly,
labelling, scoring, walk-forward) is shared; production registers the
strategy_families.yaml SIGNAL roster and captures one final prediction per day.

Pipeline:
  1. build_base() reads the shared feature store (output/feature_store/
     NIFTY_base.csv), appends any newly-resolved day from the DB, and labels every
     resolved day (actual_trade_label).
  2. Any current day whose next-day outcome does not exist yet is also loaded so
     the cascade can still PREDICT it (it just cannot be graded â€” handy for the
     daily pre-market run).
  3. The registry-authorized strategy chooser produces one final_prediction per day.
  4. output/backtest/NIFTY/production/NIFTY_prediction.csv keeps the historical
     prices, volume, India VIX, the final_prediction and the
     actual_trade_label (the technical feature columns are dropped â€” they live in
     the shared feature store).
  5. NIFTY_prediction_summary.txt captures precision / recall / accuracy of the
     final prediction (in-sample headline + an honest walk-forward number).

Run directly (the daily job, scripts/daily_NIFTY/daily_nifty_prediction.py, is the
production entrypoint that also persists to the DB):
    python -m src.technical_analysis.cascade.pipeline
    python src/technical_analysis/cascade/pipeline.py --output <path>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

_repo_root = Path(__file__).resolve().parents[3]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

load_dotenv(_repo_root / ".env")

# â”€â”€ shared cascade engine (single source of truth, shared with the experiment) â”€
# Production registers ONLY the production signal roster; the research harness
# (backtest/vectorbt_research/strategy_grid.py) registers the full roster on the same
# engine, so the two pipelines share the engine yet diverge on strategies.
from src.technical_analysis.cascade.constants import (
    _VIX_COLS, _BASE_STR_COLS, WF_WINDOW, PRODUCTION_BACKTEST_START,
    CALL, PUT, FLAT,
)
from src.technical_analysis.cascade.dataset import build_base, scoring_frame, load_vix
from src.technical_analysis.cascade.engine import (
    _fmt, score_final, _confusion_lines,
    gather_signals, build_cascade, walk_forward,
)
from src.technical_analysis.cascade.global_index_features import (
    add_global_index_features,
    build_gap_gate_signal,
    load_global_index_rows,
)
from src.technical_analysis.cascade.option_signal_mapper import enrich_option_signal_columns
from src.technical_analysis.cascade.strategies import (
    ALL_PARTICIPATING_FAMILIES,
)

# â”€â”€ pipeline-only imports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from src.common.config import get_gap_guard_pct, get_settings, get_underlying_lookback_days
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.db.supabase_client import SupabaseDatabaseClient


DEFAULT_OUTPUT = Path("output") / "backtest" / "NIFTY" / "production" / "NIFTY_prediction.csv"
STRATEGY_FIRE_LOG_OUTPUT = Path("output") / "backtest" / "NIFTY" / "production" / "NIFTY_strategy_fire_log.csv"

# Columns kept in the production CSV: the raw market data (prices, volume, India
# VIX), the cascade's final_prediction and the realised
# actual_trade_label. Every technical feature column from the feature store is
# dropped â€” those belong to research, not to the production prediction record.
_PRODUCTION_COLS = [
    "signal_date", "next_trade_date",
    "open_915", "high_day", "low_day", "close_1515",
    "volume_day",
    "vix_close", "vix_chg_1d", "vix_chg_pct",
    "next_open", "next_high", "next_low", "next_close", "next_return_pct",
    "final_prediction", "effective_prediction",
    "direction",
    "stock_regime",
    "primary_strategy", "primary_strategy_family", "primary_strategy_type",
    "strategy_precision", "signal_style",
    "strength_score", "strength_label", "confidence_level",
    "expected_move_pct", "is_option_eligible", "option_bias", "conflict_flag",
    "actual_trade_label",
    "bull_score", "bear_score", "signal_quality", "actual_quality_label",
    "quality_horizon_days",
    "global_risk_off",
    "global_gate_reason",
    "global_us_return_mean",
    "global_europe_return_mean",
    "global_asia_partial_return_mean",
    "global_asia_overnight_return_mean",
    "event_gate_reason",
    "gap_gate_reason",
]

# Families suppressed entirely on event impact days.
_EVENT_SUPPRESS_FAMILIES: frozenset[str] = frozenset({
    "OversoldBounceCall", "BollingerMeanReversion", "CalmFadePut",
})


def _generate_strategy_fire_log(
    df: pd.DataFrame,
    signals: dict[str, pd.Series],
    output_path: Path = STRATEGY_FIRE_LOG_OUTPUT,
) -> Path:
    """Write a debugging CSV: one row per (signal_date, strategy_variant) that fired.

    Captures every production SIGNAL variant that returned CALL or PUT on each
    signal date.  Purely for inspection — no effect on cascade logic, metrics, or DB.

    Columns:
        signal_date, strategy_variant, strategy_family, strategy_type, direction
    """
    from src.technical_analysis.strategy_families import get_strategy_family_registry

    registry = get_strategy_family_registry()
    signal_dates = df["signal_date"].tolist()

    rows: list[dict] = []
    for name, sig in signals.items():
        try:
            meta = registry.get_meta(name)
        except KeyError:
            continue
        if meta.strategy_type != "SIGNAL":
            continue
        for pos, idx in enumerate(df.index):
            direction = sig.loc[idx] if idx in sig.index else "NO_POSITION"
            if direction not in ("CALL", "PUT"):
                continue
            rows.append({
                "signal_date": signal_dates[pos],
                "strategy_variant": name,
                "strategy_family": meta.family,
                "strategy_type": meta.strategy_type,
                "direction": direction,
            })

    fire_df = (
        pd.DataFrame(rows, columns=[
            "signal_date", "strategy_variant",
            "strategy_family", "strategy_type", "direction",
        ])
        .drop_duplicates(["signal_date", "strategy_variant"])
        .sort_values(["signal_date", "strategy_family", "strategy_variant"])
        .reset_index(drop=True)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fire_df.to_csv(output_path, index=False)
    print(f"Strategy fire log: {len(fire_df)} rows -> {output_path}")
    return output_path


def _apply_event_gate(
    full: pd.DataFrame,
    final_pos: pd.Series,
    signals: dict[str, pd.Series],
    elig: dict | None = None,
) -> pd.Series:
    """Return a guarded prediction for macro-event impact days.

    Stamps full["event_gate_reason"] for audit without mutating final_prediction.
    Outside calendar coverage or unparseable dates are silently skipped.
    """
    from scripts.Common.event_calendar import is_event_impact_day, EventCalendarCoverageError
    from src.technical_analysis.strategy_families import get_strategy_family_registry

    registry = get_strategy_family_registry()
    result = final_pos.copy()

    for idx in full.index:
        if result.loc[idx] == "NO_POSITION":
            continue
        try:
            td = pd.to_datetime(full.loc[idx, "next_trade_date"]).date()
            if not is_event_impact_day(td, buffer_days=0):
                continue
        except (EventCalendarCoverageError, Exception):
            continue  # outside coverage or bad date — don’t block

        direction = result.loc[idx]
        primary_family: str | None = None
        for name, sig in signals.items():
            if idx in sig.index and sig.loc[idx] == direction:
                try:
                    meta = registry.get_meta(name)
                    if meta.strategy_type == "SIGNAL":
                        primary_family = meta.family
                        break
                except KeyError:
                    pass

        result.loc[idx] = "NO_POSITION"
        tag = "SUPPRESS" if primary_family in _EVENT_SUPPRESS_FAMILIES else "DEMOTE"
        full.loc[idx, "event_gate_reason"] = f"{tag}:{primary_family or 'unknown'}"

    return result


def _apply_guard_layer(
    full: pd.DataFrame,
    final_prediction: pd.Series,
    signals: dict[str, pd.Series],
) -> pd.Series:
    """Apply post-cascade guards and return effective_prediction."""
    full["event_gate_reason"] = ""
    effective_pos = _apply_event_gate(full, final_prediction, signals)

    full["gap_gate_reason"] = ""
    next_open_s = pd.to_numeric(
        full["next_open"] if "next_open" in full.columns else pd.Series(dtype=float),
        errors="coerce",
    )
    close_s = pd.to_numeric(full["close_1515"], errors="coerce")
    gap_guard_pct = get_gap_guard_pct()
    gap_up_mask = ((next_open_s / close_s - 1) > gap_guard_pct).fillna(False)
    gap_down_mask = ((close_s / next_open_s - 1) > gap_guard_pct).fillna(False)
    call_mask = (effective_pos == CALL) & gap_up_mask
    put_mask = (effective_pos == PUT) & gap_down_mask

    guarded = effective_pos.copy()
    guarded.loc[call_mask] = FLAT
    guarded.loc[put_mask] = FLAT
    full.loc[call_mask, "gap_gate_reason"] = "GAP_UP"
    full.loc[put_mask, "gap_gate_reason"] = "GAP_DOWN"
    return guarded


def _apply_global_gate(full: pd.DataFrame) -> pd.DataFrame:
    """Optional final-layer global index gate for cascade final_prediction.

    This helper is intentionally not called by the current production path. It is
    retained for the future design where global indices may suppress either a
    trade-eligible trigger or a watch-only trigger after promotion.

    Two layers, applied in order (later layers can further block already-open signals):

    1. Same-day gate — uses global_risk_off / global_risk_on columns (already computed
       by add_global_index_features from point-in-time global session returns) plus
       the 3 regional means.  Fires on every trading day
       where the precomputed global backdrop disagrees with the prediction direction.

    2. Holiday gap gate — when trade_date is more than 1 calendar day after the
       previous row's trade_date (multi-day Indian holiday), cumulative GlobalIndexOhlc
       returns over the gap are computed and the combined risk_off/put_agree gate is
       applied.  Catches scenarios where the same-day 1d signal is weak but the
       accumulated gap effect is severe.

    Gate logic (same in both layers):
       CALL blocked if risk_off  OR put_agree  (2+ of 3 regions negative)
       PUT  blocked if risk_on   OR call_agree (2+ of 3 regions positive)

    Overrides final_prediction -> NO_POSITION where blocked.
    Sets global_gate_reason to the trigger name; empty string if not blocked.
    The cascade direction column is preserved to show what the raw signal was.
    """
    out = full.copy()
    out["global_gate_reason"] = ""

    # --- Layer 1: same-day gate using precomputed feature columns ---
    regional = out[
        ["global_us_return_mean", "global_europe_return_mean", "global_asia_overnight_return_mean"]
    ].apply(pd.to_numeric, errors="coerce")
    put_agree_s = (regional < 0).sum(axis=1) >= 2
    call_agree_s = (regional > 0).sum(axis=1) >= 2
    risk_off_s = out["global_risk_off"].fillna(0).astype(bool)
    risk_on_s = out["global_risk_on"].fillna(0).astype(bool)

    call_rows = out["final_prediction"] == "CALL"
    put_rows = out["final_prediction"] == "PUT"

    mask = call_rows & risk_off_s
    out.loc[mask, "final_prediction"] = "NO_POSITION"
    out.loc[mask, "global_gate_reason"] = "RISK_OFF"

    mask = call_rows & ~risk_off_s & put_agree_s
    out.loc[mask, "final_prediction"] = "NO_POSITION"
    out.loc[mask, "global_gate_reason"] = "PUT_AGREE"

    mask = put_rows & risk_on_s
    out.loc[mask, "final_prediction"] = "NO_POSITION"
    out.loc[mask, "global_gate_reason"] = "RISK_ON"

    mask = put_rows & ~risk_on_s & call_agree_s
    out.loc[mask, "final_prediction"] = "NO_POSITION"
    out.loc[mask, "global_gate_reason"] = "CALL_AGREE"

    # --- Layer 2: holiday gap gate using point-in-time global returns over the gap ---
    out["_trade_dt"] = pd.to_datetime(out["signal_date"])
    prev_dt = out["_trade_dt"].shift(1)
    gap_calendar_days = (out["_trade_dt"] - prev_dt).dt.days
    gap_row_idx = out.index[gap_calendar_days > 1]

    if len(gap_row_idx) > 0:
        min_load = (out.loc[gap_row_idx, "_trade_dt"].min() - pd.Timedelta(days=14)).date()
        max_load = out.loc[gap_row_idx, "_trade_dt"].max().date()
        try:
            global_rows = load_global_index_rows(min_load, max_load)
            global_rows["trade_date"] = pd.to_datetime(global_rows["trade_date"])
        except Exception as exc:
            print(f"[WARN] Global gap gate: GlobalIndexOhlc load failed: {exc}")
            global_rows = pd.DataFrame(columns=["index_code", "trade_date", "close_price"])

        for idx in gap_row_idx:
            direction = out.loc[idx, "final_prediction"]
            if direction not in ("CALL", "PUT"):
                continue  # already gated or flat
            trade_dt = out.loc[idx, "_trade_dt"]
            prior_dt = prev_dt.loc[idx]
            gap_data = global_rows[
                (global_rows["trade_date"] >= prior_dt) &
                (global_rows["trade_date"] < trade_dt)
            ]
            gate = build_gap_gate_signal(gap_data)
            call_blocked = direction == "CALL" and (gate["risk_off"] or gate["put_agree"])
            put_blocked = direction == "PUT" and (gate["risk_on"] or gate["call_agree"])
            if call_blocked:
                trigger = "GAP_RISK_OFF" if gate["risk_off"] else "GAP_PUT_AGREE"
            elif put_blocked:
                trigger = "GAP_RISK_ON" if gate["risk_on"] else "GAP_CALL_AGREE"
            else:
                continue
            out.loc[idx, "final_prediction"] = "NO_POSITION"
            out.loc[idx, "global_gate_reason"] = trigger

    return out.drop(columns=["_trade_dt"])




def _quality_mean_interpretation(mean_quality: float | None) -> str:
    if mean_quality is None or pd.isna(mean_quality):
        return "n/a"
    if mean_quality >= 0.25:
        return "strongly positive directional edge"
    if mean_quality >= 0.10:
        return "moderate positive directional edge"
    if mean_quality >= 0.02:
        return "slightly positive, close-to-neutral directional edge"
    if mean_quality > -0.02:
        return "neutral / no clear directional edge"
    if mean_quality > -0.10:
        return "slightly negative, close-to-neutral directional edge"
    if mean_quality > -0.25:
        return "moderate negative directional edge"
    return "strongly negative directional edge"


def _positive_quality_interpretation(positive_rate: float | None) -> str:
    if positive_rate is None or pd.isna(positive_rate):
        return "n/a"
    if positive_rate >= 60:
        return "most fired signals aligned with the stronger 3-day move"
    if positive_rate >= 55:
        return "a modest majority of fired signals aligned"
    if positive_rate >= 45:
        return "mixed; roughly balanced positive vs negative fired signals"
    if positive_rate >= 40:
        return "a modest minority of fired signals aligned"
    return "most fired signals did not align with the stronger 3-day move"


def _quality_interpretation_line(mean_quality: float | None, positive_rate: float | None) -> str:
    if (mean_quality is None or pd.isna(mean_quality)) and (
        positive_rate is None or pd.isna(positive_rate)
    ):
        return "  quality read     : n/a"
    return (
        "  quality read     : "
        f"mean={_quality_mean_interpretation(mean_quality)}; "
        f"positive%={_positive_quality_interpretation(positive_rate)}"
    )


def _final_block(
    title: str,
    m: dict,
    quality: dict | None = None,
    quality_label: dict | None = None,
) -> list[str]:
    lines = [
        title,
        "-" * 64,
        f"  fires            : {m['n_call'] + m['n_put']} "
        f"(CALL {m['n_call']}, PUT {m['n_put']}, FLAT {m['n_flat']}) of {m['n']} days",
        f"  precision        : {_fmt(m['dir_precision'])}   "
        f"(naive always-PUT {_fmt(m['put_base'])}, lift {_fmt(m['lift'])}x)",
        f"  recall           : {_fmt(m['dir_recall'])}   "
        f"(fires / {m['n_move']} actual-move days)",
        f"  wrong-way rate   : {_fmt(m['wrong_way_rate'])}   "
        f"(took a side, opposite move happened)",
        f"  overall accuracy : {_fmt(m['overall_accuracy'])}   "
        f"(correct fires + correct NO_POSITION / all days)",
    ]
    if quality is not None:
        mean_quality = quality.get("mean_signal_quality")
        median_quality = quality.get("median_signal_quality")
        positive_rate = quality.get("positive_quality_rate_pct")
        lines += [
            f"  quality scored   : {quality.get('quality_scored_fires', 0)} fires "
            "(complete T+1..T+3 outcomes)",
            f"  mean quality     : {mean_quality:.3f}" if mean_quality is not None else "  mean quality     : n/a",
            f"  median quality   : {median_quality:.3f}" if median_quality is not None else "  median quality   : n/a",
            f"  positive quality : {positive_rate:.1f}%" if positive_rate is not None else "  positive quality : n/a",
            _quality_interpretation_line(mean_quality, positive_rate),
        ]
    if quality_label is not None:
        qp = quality_label.get("qualityBased_precision")
        qr = quality_label.get("qualityBased_recall")
        qf = quality_label.get("qualityBased_F1")
        lines += [
            f"  qualityBased precision : {qp:.3f}" if qp is not None else "  qualityBased precision : n/a",
            f"  qualityBased recall    : {qr:.3f}" if qr is not None else "  qualityBased recall    : n/a",
            f"  qualityBased F1        : {qf:.3f}" if qf is not None else "  qualityBased F1        : n/a",
        ]
    lines.append("")
    return lines


def _write_prediction_summary(
    df_res: pd.DataFrame,
    effective_pred_res: pd.Series,
    pending: pd.DataFrame,
    summary_path: Path,
) -> None:
    """Write precision / recall / accuracy of effective_prediction. Graded on the
    resolved history (in-sample headline + walk-forward out-of-sample)."""
    from src.technical_analysis.prediction.signal_strength import (
        add_raw_direction, quality_label_metrics, summarize_signal_quality,
    )

    outcomes = add_raw_direction(df_res)
    m_in = score_final(df_res, effective_pred_res)
    q_in = summarize_signal_quality(effective_pred_res, outcomes)
    ql_in = quality_label_metrics(effective_pred_res, outcomes["actual_quality_label"])
    lines = [
        f"graded rows: {m_in['n']}   "
        f"date range: {df_res['signal_date'].min()} .. {df_res['signal_date'].max()}",
        "",
    ]

    lines += _final_block("Effective prediction — in-sample (optimistic)",
                          m_in, q_in, ql_in)
    lines.append("  Effective-prediction confusion matrix:")
    lines += _confusion_lines(df_res, effective_pred_res)
    lines.append("")

    # Walk-forward (honest out-of-sample) uses trailing precision only as a
    # conflict tie-break; strategy participation still comes from the registry.
    rs_res = gather_signals(df_res, ALL_PARTICIPATING_FAMILIES)
    wf_pred = walk_forward(df_res, rs_res)
    wf_eval = df_res.iloc[WF_WINDOW:]
    if len(wf_eval):
        wf_effective = wf_pred.loc[wf_eval.index]
        wf = score_final(wf_eval, wf_effective)
        wf_quality = summarize_signal_quality(
            wf_effective, outcomes.loc[wf_eval.index]
        )
        wf_quality_label = quality_label_metrics(
            wf_effective, outcomes.loc[wf_eval.index, "actual_quality_label"]
        )
        lines += _final_block(
            f"Effective prediction — walk-forward (rolling {WF_WINDOW}-day eligibility, "
            "sequential watch state, out-of-sample — "
            "the honest number)",
            wf,
            wf_quality,
            wf_quality_label,
        )
        lines.append("  Walk-forward confusion matrix:")
        lines += _confusion_lines(wf_eval, wf_effective)
        lines.append("")

    lines.append("Caveat: in-sample eligibility is fit on the same history it grades, so")
    lines.append("the in-sample headline is optimistic; the walk-forward number is the")
    lines.append("honest read. Research, not a live trading signal.")
    lines.append("")

    text = "\n".join(lines)
    print(text)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(text, encoding="utf-8")
    print(f"Summary written to {summary_path}")


def generate_prediction_csv(
    underlying: str = "NIFTY",
    output_path: Path = DEFAULT_OUTPUT,
    summary_path: Path | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    **_legacy_kwargs: Any,
) -> dict[str, Any]:
    """Run the precision cascade over the full NIFTY history (plus any
    current unresolved day) and write the production prediction CSV + summary.

    The final prediction uses the shared cascade engine
    (src/technical_analysis/cascade) with the PROMOTED strategy roster â€” the same
    engine the research harness drives with the full roster. Extra legacy keyword
    arguments are accepted and ignored for backward compatibility with older
    callers."""
    if underlying.upper() != "NIFTY":
        raise ValueError("The cascade production pipeline currently supports NIFTY only.")

    output_path = Path(output_path)
    if summary_path is None:
        summary_path = output_path.with_name(output_path.stem + "_summary.txt")
    else:
        summary_path = Path(summary_path)

    # 1) Load full history from DB (includes today's unresolved row if market closed).
    #    build_base() reads all SignalFeatureDaily rows, joins VIX + global index
    #    features across the full date range, and classifies regimes. No CSV involved.
    full_base = build_base().reset_index(drop=True)

    # 2) Split into resolved (next_open known — D+1 candle exists, row is gradeable)
    #    and unresolved (today's signal — next_trade_date open not yet captured).
    in_production = pd.to_datetime(full_base["signal_date"]) >= pd.Timestamp(PRODUCTION_BACKTEST_START)
    resolved = full_base[in_production & full_base["next_open"].notna()].reset_index(drop=True)
    unresolved = full_base[full_base["next_open"].isna()].reset_index(drop=True)

    if not unresolved.empty:
        print(f"  {len(unresolved)} unresolved signal date(s) (next_trade_date open not yet captured): "
              f"{', '.join(unresolved['signal_date'])}")

    full = pd.concat([resolved, unresolved], ignore_index=True) if not unresolved.empty else resolved.copy()
    n_res = len(resolved)
    resolved_full = full.iloc[:n_res]

    # 3) Production strategy chooser. Any single SIGNAL strategy can create a
    # CALL/PUT prediction; when both sides fire, the side with the stronger
    # historical precision wins.
    all_signals = gather_signals(full, ALL_PARTICIPATING_FAMILIES)
    _generate_strategy_fire_log(full, all_signals)
    final_pos, strategy_precisions = build_cascade(
        full, all_signals, scoring_frame(resolved_full)
    )
    full = full.copy()

    full["final_prediction"] = final_pos
    full["effective_prediction"] = _apply_guard_layer(full, full["final_prediction"], all_signals)

    # Option-selection metadata follows the actionable signal.
    full = enrich_option_signal_columns(
        full, full["effective_prediction"], all_signals, strategy_precisions
    )

    # Realised outcome scores. These require future sessions and are persisted only
    # as grading/audit fields on NiftyPrediction; they are never strategy inputs.
    from src.technical_analysis.prediction.signal_strength import add_raw_direction

    quality_horizon = get_underlying_lookback_days()
    full = add_raw_direction(full, horizon=quality_horizon)
    full["signal_quality"] = full["raw_signal_quality"]
    full["quality_horizon_days"] = quality_horizon

    # 3b) Global prediction gate disabled. Global context is persisted for audit,
    #     but no strategy-level global suppressor is active.
    full["global_gate_reason"] = ""

    # 4) assemble output dataframe — DB is the durable store; no CSV written.
    date_mask = pd.Series(True, index=full.index)
    if start is not None:
        date_mask &= pd.to_datetime(full["signal_date"]) >= pd.Timestamp(start)
    if end is not None:
        date_mask &= pd.to_datetime(full["signal_date"]) <= pd.Timestamp(end)
    output_full = full.loc[date_mask].copy()
    out_df = output_full.reindex(columns=_PRODUCTION_COLS).copy()
    print(f"Prepared {len(out_df)} prediction rows")

    # 5) summary -- always graded on the FULL resolved history from PRODUCTION_BACKTEST_START,
    #    regardless of start/end args (which only scope the DB upsert / CSV output).
    #    full.iloc[:n_res] is the resolved slice of full with predictions already applied.
    resolved_for_summary = full.iloc[:n_res].reset_index(drop=True)
    effective_for_summary = resolved_for_summary["effective_prediction"].reset_index(drop=True)
    pending = out_df[pd.to_numeric(out_df["next_open"], errors="coerce").isna()]
    try:
        _write_prediction_summary(
            resolved_for_summary,
            effective_for_summary,
            pending,
            summary_path,
        )
    except Exception as exc:  # noqa: BLE001 - summary output is local-dashboard convenience.
        print(f"[WARN] Prediction summary write skipped: {type(exc).__name__}: {exc}")

    return {
        "rows": len(out_df),
        "path": str(output_path),
        "summary_path": str(summary_path),
        "graded_rows": int(len(resolved_for_summary)),
        "pending_predicted": int(len(pending)),
        "strategy_fire_log_path": str(STRATEGY_FIRE_LOG_OUTPUT),
        "frame": out_df,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the NIFTY production final-prediction CSV via the "
                    "precision cascade.")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"Output CSV path. Default: {DEFAULT_OUTPUT}")
    parser.add_argument("--summary", default=None,
                        help="Summary txt path. Default: <output>_summary.txt")
    parser.add_argument("--start", default=None, help="Only write/upsert signal_date >= this date (YYYY-MM-DD).")
    parser.add_argument("--end", default=None, help="Only write/upsert signal_date <= this date (YYYY-MM-DD).")
    args = parser.parse_args()

    result = generate_prediction_csv(
        underlying=args.underlying.upper(),
        output_path=Path(args.output),
        summary_path=Path(args.summary) if args.summary else None,
        start=args.start,
        end=args.end,
    )
    print(result)


if __name__ == "__main__":
    main()


