"""
NIFTY production prediction pipeline â€” regime-aware precision cascade.

This is the PRODUCTION counterpart to the research harness in
backtest/vectorbt_research/strategy_grid.py. The cascade engine (dataset assembly,
labelling, precision-floor voting, scoring, walk-forward) is shared; production
registers ONLY the promoted strategy roster and captures the single final
prediction per day.

Pipeline:
  1. build_base() reads the shared feature store (output/feature_store/
     NIFTY_base.csv), appends any newly-resolved day from the DB, and labels every
     resolved day (actual_trade_label).
  2. Any current day whose next-day outcome does not exist yet is also loaded so
     the cascade can still PREDICT it (it just cannot be graded â€” handy for the
     daily pre-market run).
  3. The regime-aware precision cascade (eligibility fit on resolved history only)
     produces one final_prediction per day.
  4. output/backtest/NIFTY/production/NIFTY_prediction.csv keeps the historical
     prices, volume, India VIX, regime, the final_prediction and the
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
# Production registers ONLY the promoted strategy roster; the research harness
# (backtest/vectorbt_research/strategy_grid.py) registers the full roster on the same
# engine, so the two pipelines share the engine yet diverge on strategies.
from src.technical_analysis.cascade.constants import (
    _VIX_COLS, _BASE_STR_COLS, WF_WINDOW, PRODUCTION_BACKTEST_START,
)
from src.technical_analysis.cascade.dataset import (
    build_base, regime_frame, classify_regime, load_vix,
)
from src.technical_analysis.cascade.engine import (
    _fmt, score_final, _confusion_lines,
    gather_regime_signals, build_regime_cascade, walk_forward_regime,
)
from src.technical_analysis.cascade.global_index_features import (
    add_global_index_features,
    build_gap_gate_signal,
    load_global_index_rows,
)
from src.technical_analysis.cascade.option_signal_mapper import enrich_option_signal_columns
from src.technical_analysis.cascade.strategies import PROMOTED_REGIME_FAMILIES

# â”€â”€ pipeline-only imports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from src.common.config import get_settings, get_underlying_lookback_days
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.db.supabase_client import SupabaseDatabaseClient


DEFAULT_OUTPUT = Path("output") / "backtest" / "NIFTY" / "production" / "NIFTY_prediction.csv"

# Columns kept in the production CSV: the raw market data (prices, volume, India
# VIX), the volatility regime, the cascade's final_prediction and the realised
# actual_trade_label. Every technical feature column from the feature store is
# dropped â€” those belong to research, not to the production prediction record.
_PRODUCTION_COLS = [
    "signal_date", "next_trade_date",
    "open_915", "high_day", "low_day", "close_1515",
    "volume_day",
    "vix_close", "vix_chg_1d", "vix_chg_pct",
    "regime",
    "next_open", "next_high", "next_low", "next_close", "next_return_pct",
    "final_prediction",
    "direction",
    "volatility_regime", "stock_regime",
    "primary_strategy", "strategy_precision", "signal_style",
    "strength_score", "strength_label", "confidence_level",
    "expected_move_pct", "is_option_eligible", "option_bias", "conflict_flag",
    "actual_trade_label",
    "bull_score", "bear_score", "signal_quality", "actual_quality_label",
    "quality_horizon_days",
    "global_risk_off",
    "global_gate_reason",
    "global_us_return_mean",
    "global_europe_return_mean",
    "global_asia_return_mean",
]


def _apply_global_gate(full: pd.DataFrame) -> pd.DataFrame:
    """Apply global index gate to cascade final_prediction.

    Two layers, applied in order (later layers can further block already-open signals):

    1. Same-day gate — uses global_risk_off / global_risk_on columns (already computed
       by add_global_index_features from prior-session 1d returns) plus a
       GlobalNoDisagree check on the 3 regional means.  Fires on every trading day
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
        ["global_us_return_mean", "global_europe_return_mean", "global_asia_return_mean"]
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

    # --- Layer 2: holiday gap gate using cumulative returns over the gap ---
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
        f"(correct fires / {m['n_move']} actual-move days)",
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
    pred_res: pd.Series,
    pending: pd.DataFrame,
    summary_path: Path,
) -> None:
    """Write precision / recall / accuracy of the final prediction. Graded on the
    resolved history (in-sample headline + walk-forward out-of-sample)."""
    from src.technical_analysis.prediction.signal_strength import (
        add_raw_direction, quality_label_metrics, summarize_signal_quality,
    )

    outcomes = add_raw_direction(df_res)
    m_in = score_final(df_res, pred_res)
    q_in = summarize_signal_quality(pred_res, outcomes)
    ql_in = quality_label_metrics(pred_res, outcomes["actual_quality_label"])
    lines = [
        f"graded rows: {m_in['n']}   "
        f"date range: {df_res['signal_date'].min()} .. {df_res['signal_date'].max()}",
        "",
    ]

    lines += _final_block("In-sample (eligibility fit + graded on the same history; optimistic)",
                          m_in, q_in, ql_in)
    lines.append("  Confusion matrix:")
    lines += _confusion_lines(df_res, pred_res)
    lines.append("")

    # Walk-forward (honest out-of-sample) â€” eligibility fit only on trailing days.
    rs_res = gather_regime_signals(df_res, PROMOTED_REGIME_FAMILIES)
    wf_pred = walk_forward_regime(df_res, rs_res)
    wf_eval = df_res.iloc[WF_WINDOW:]
    if len(wf_eval):
        wf = score_final(wf_eval, wf_pred.loc[wf_eval.index])
        wf_quality = summarize_signal_quality(
            wf_pred.loc[wf_eval.index], outcomes.loc[wf_eval.index]
        )
        wf_quality_label = quality_label_metrics(
            wf_pred.loc[wf_eval.index], outcomes.loc[wf_eval.index, "actual_quality_label"]
        )
        lines += _final_block(
            f"Walk-forward (rolling {WF_WINDOW}-day eligibility, out-of-sample â€” "
            "the honest number)",
            wf,
            wf_quality,
            wf_quality_label,
        )
        lines.append("  Walk-forward confusion matrix:")
        lines += _confusion_lines(wf_eval, wf_pred.loc[wf_eval.index])
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
    **_legacy_kwargs: Any,
) -> dict[str, Any]:
    """Run the regime-aware precision cascade over the full NIFTY history (plus any
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

    # 3) cascade: eligibility fit on resolved rows only; predict every row.
    regime_signals = gather_regime_signals(full, PROMOTED_REGIME_FAMILIES)
    elig_frames = {r: regime_frame(resolved_full, r) for r in PROMOTED_REGIME_FAMILIES}
    final_pos, elig = build_regime_cascade(full, regime_signals, elig_frames)
    full = full.copy()
    full["final_prediction"] = final_pos
    full = enrich_option_signal_columns(full, final_pos, regime_signals, elig)

    # Realised outcome scores. These require future sessions and are persisted only
    # as grading/audit fields on NiftyPrediction; they are never strategy inputs.
    from src.technical_analysis.prediction.signal_strength import add_raw_direction

    quality_horizon = get_underlying_lookback_days()
    full = add_raw_direction(full, horizon=quality_horizon)
    full["signal_quality"] = full["raw_signal_quality"]
    full["quality_horizon_days"] = quality_horizon

    # 3b) global gate disabled — global context is already embedded in strategy variants
    #     via GlobalNoDisagree (2-of-3 regional breadth). Set gate reason to empty string.
    full["global_gate_reason"] = ""

    # 4) assemble output dataframe — DB is the durable store; no CSV written.
    out_df = full.reindex(columns=_PRODUCTION_COLS).copy()
    print(f"Prepared {len(out_df)} prediction rows")

    # 5) summary â€” precision / recall graded on the resolved history.
    pending = out_df.iloc[n_res:]
    try:
        _write_prediction_summary(resolved_full, final_pos.iloc[:n_res], pending, summary_path)
    except Exception as exc:  # noqa: BLE001 - summary output is local-dashboard convenience.
        print(f"[WARN] Prediction summary write skipped: {type(exc).__name__}: {exc}")

    return {
        "rows": len(out_df),
        "path": str(output_path),
        "summary_path": str(summary_path),
        "graded_rows": n_res,
        "pending_predicted": int(len(unresolved)),
        "frame": out_df,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the NIFTY production final-prediction CSV via the "
                    "regime-aware precision cascade.")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"Output CSV path. Default: {DEFAULT_OUTPUT}")
    parser.add_argument("--summary", default=None,
                        help="Summary txt path. Default: <output>_summary.txt")
    args = parser.parse_args()

    result = generate_prediction_csv(
        underlying=args.underlying.upper(),
        output_path=Path(args.output),
        summary_path=Path(args.summary) if args.summary else None,
    )
    print(result)


if __name__ == "__main__":
    main()
