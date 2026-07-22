from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import html
import json as _json_mod
from pathlib import Path
import re
import threading
import uuid
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, url_for

from src.common.config import get_settings, get_trade_horizon_days, get_target_pct_for_regime, get_sl_pct_for_regime

load_dotenv(Path(".env"))

NIFTY_SYMBOL = "NIFTY"
MODEL_VERSION = "cascade_v1"
BASE_OUTPUT_DIR = Path("output") / "backtest" / NIFTY_SYMBOL
RESEARCH_OUTPUT_DIR = BASE_OUTPUT_DIR / "vectorbt_research"
PRODUCTION_OUTPUT_DIR = BASE_OUTPUT_DIR / "production"
TRADES_OUTPUT_DIR = BASE_OUTPUT_DIR / "vectorbt"
RESEARCH_DEFAULT_START = date(2026, 1, 1)
TARGET_PCT_OPTIONS = [0.01, 0.02, 0.05, 0.07, 0.10]
STOP_LOSS_PCT_OPTIONS = [0.01, 0.02, 0.03, 0.05]
STRATEGY_TYPE_TOOLTIPS = {
    "TRADE_ELIGIBLE": (
        "Production strategy: can directly generate trades and can create or "
        "confirm watches."
    ),
    "WATCH_ONLY": (
        "Production strategy: can create or confirm watches, but cannot directly "
        "generate a trade without promotion."
    ),
    "RESEARCH": (
        "Research-grid only: excluded from production trading and watch/promotion logic."
    ),
}
RESEARCH_OUTPUT_FILES = {
    "summary": "strategy_grid_summary.txt",
    "leaderboard": "strategy_grid_leaderboard.csv",
    "predictions": "strategy_grid_predictions.csv",
    "trades": "strategy_grid_trades.csv",
    "plans": "strategy_grid_trade_plans.csv",
    "definitions": "strategy_grid_definitions.csv",
    "watch_promotions": "strategy_grid_watch_promotions.csv",
}
# ---------------------------------------------------------------------------
# Column-header tooltips for the Daily Prediction & Option Selection table
# Keys match the lowercase dict keys returned by format_signal_row().
# ---------------------------------------------------------------------------
PRODUCTION_COLUMN_TOOLTIPS: dict[str, str] = {
    "signal_strength":     "Signal strength score (0-100) of the primary firing strategy. STRONG ≥ 80, MODERATE ≥ 65, WEAK < 65. Computed from base_score + feature adjustments in signal_strength_config.yaml.",
    "signal_date":        "Date the prediction signal was generated (signal observation day, D).",
    "trade_date":         "Execution session — the next trading day (D+1) when the option trade would open.",
    "predicted":          "Effective cascade prediction: CALL, PUT or NO_POSITION. Includes watch-promoted signals.",
    "us_ret":             "US equity market return on signal date (overnight macro context).",
    "europe_ret":         "Europe equity market return on signal date (overnight macro context).",
    "asia_ret":           "Asia overnight gap: D-1 final close -> D open (~7 AM IST).",
    "asia_partial_ret":   "Asia intraday return: open(D) to partial close ~9:20 AM IST.",
    "asia_overnight_ret": "Asia overnight gap: D-1 final close to D open (~7 AM IST).",
    "drift_prediction":   "drift_effective_prediction: final direction after 9:22 AM drift overrule (CALL/PUT/NO_POSITION).",
    "drift_size":         "drift_position_size_pct: position size fraction after overrule (0.5=half, 1.0=full).",
    "drift_reason":       "drift_overrule_reason: DRIFT_CONFIRMS_HALF_SIZE | DRIFT_CONFIRMS_FULL | DRIFT_OPPOSES | DRIFT_PROMOTES_WATCH | DRIFT_PROBE | TAIL_SHOCK | NO_CHANGE.",
    "actual_label": (
        "Actual NIFTY movement outcome over 3 sessions from trade-date open (next_open).\n"
        "Threshold = clip(0.55 \u00d7 ATR14 / close_1515, 0.4%, 1.2%) per row \u2014 adapts to current volatility.\n"
        "CALL if future_high_3d \u2265 next_open\u00d7(1+thr); PUT if future_low_3d \u2264 next_open\u00d7(1-thr); BOTH if both."
    ),
    "quality_label": (
        "Signal quality label. Threshold = 0.5 \u00d7 ATR14_SMA (half an ATR from signal-date close).\n"
        "bull_score = (future_high_3d - close) / ATR; bear_score = (close - future_low_3d) / ATR.\n"
        "CALL if bull > 0.5 AND (bull-bear)/(bull+bear) > 0; PUT if bear > 0.5 AND ratio < 0."
    ),
    "max_underlying_up":  "Max NIFTY upside over 3 sessions from trade-date open: (future_high \u2212 next_open) / next_open.",
    "max_underlying_down":"Max NIFTY downside over 3 sessions from trade-date open: (next_open \u2212 future_low) / next_open.",
    "regime":             "Volatility regime on signal date: stress (VIX \u2265 16 OR vol_10d \u2265 0.7%) or calm.",
    "prediction_strategy":"Primary strategy that drove the effective prediction.",
    "watched_strategy":   "Watch/confirming strategy that seeded or confirmed a promoted prediction.",
    "option_selection":   "Selected option strategy type, or NO_TRADE reason if no option was selected.",
    "selected_option_score": "Composite score (0-100) of the best option contract selected. Based on IV rank, liquidity, reward/risk, and delta quality. Shown regardless of whether it met the old 65-point threshold.",
    "option_symbol":      "Option contract trading symbol selected for the trade.",
    "option_type":        "CE (Call option) or PE (Put option).",
    "strike":             "Option strike price of the selected contract.",
    "entry":              "Entry price: actual paper-trade fill if available; else planned entry from OPEN_0915 snapshot.",
    "entry_type":         "actual = live paper trade fill price; planned = from option chain OPEN_0915 snapshot.",
    "target_1":           "Target exit price = entry \u00d7 (1 + target_pct). Regime-specific target_pct set in .env.",
    "stop_loss":          "Stop-loss exit price = entry \u00d7 (1 \u2212 sl_pct). Regime-specific sl_pct set in .env.",
    "latest_option_price":"Most recent option premium from OptionSnapshot during the holding window.",
    "max_option_price":   "Highest option premium seen in the holding window before exit.",
    "min_option_price":   "Lowest option premium seen in the holding window before exit.",
    "pnl_pct":            "P&L % from entry to latest/exit price: (exit \u2212 entry) / entry \u00d7 100.",
    "pnl_points":         "P&L in premium points: exit_price \u2212 entry_price.",
    "pnl_status": (
        "Trade exit status:\n"
        "TARGET_HIT \u2014 option hit target_1_price\n"
        "STOP_LOSS_HIT \u2014 option hit stop_loss_price\n"
        "OPEN \u2014 position still open\n"
        "NO_SNAPSHOT_DATA \u2014 no intraday option prices found\n"
        "NO_OPTION_SELECTED \u2014 no option matched selection criteria\n"
        "Other \u2014 the no_trade_reason from the option selector"
    ),
    "event_gate":         "Event-gate reason that blocked a trade on this date (e.g. scheduled event risk).",
    "snapshots":          "Number of OptionSnapshot price observations during the trade holding window.",
    "last_snapshot":      "Timestamp of the most recent price observation in the holding window.",
}
PRECISION_MISSES_FILE = PRODUCTION_OUTPUT_DIR / "NIFTY_stress_in_sample_precision_misses.csv"
RECALL_MISSES_FILE = PRODUCTION_OUTPUT_DIR / "NIFTY_stress_in_sample_recall_misses.csv"
MISS_ANALYSIS_FILES = {
    "precision": PRECISION_MISSES_FILE,
    "recall": RECALL_MISSES_FILE,
}
STRATEGY_DEFINITION_PATHS = (
    Path("output") / "definitions" / "strategy_definitions.csv",
    RESEARCH_OUTPUT_DIR / "strategy_grid_definitions.csv",
)

# Internal watch/promotion lineage remains persisted for diagnostics, but is not
# rendered on any dashboard table. Users act on the single effective Predicted
# value regardless of whether it came directly from the cascade or from promotion.
UI_HIDDEN_PREDICTION_AUDIT_COLUMNS = {
    "final_prediction",
    "source_final_prediction",
    "effective_prediction",
    "watch_signal",
    "prior_watch_signal",
    "prior_watch_age",
    "promoted_prediction",
    "promotion_reason",
    "strategy_family",
    "strategy_type",
    "strategy_authority",
    "primary_strategy_family",
    "primary_strategy_type",
    "watch_family",
    "prior_watch_family",
    "prior_watch_variant",
    "prior_watch_strategy_type",
    "confirming_family",
    "confirming_variant",
    "confirming_strategy_type",
    "family_confirmation_match",
    "promotion_block_reason",
}

app = Flask(__name__)

# ── async research job registry ─────────────────────────────────────────────
# Keyed by job_id (uuid str). States: "running" | "done" | "failed".
_RESEARCH_JOBS: dict[str, dict] = {}
_RESEARCH_JOBS_LOCK = threading.Lock()

# ── production recompute job (singleton) ─────────────────────────────────────
_RECOMPUTE_JOB: dict = {"state": "idle", "message": "", "error": "", "started_at": ""}
_RECOMPUTE_LOCK = threading.Lock()


@dataclass(frozen=True)
class PageTable:
    title: str
    path: Path | None
    html: str
    rows: int
    empty_message: str = "No rows available yet."
    controls_html: str = ""
    section_id: str = ""


@app.get("/")
def index():
    return redirect(url_for("research"))


@app.get("/health")
def health():
    return {"status": "ok", "app": "stockie26-flask-ui"}


@app.get("/production/download")
def production_download():
    """Download the full production backtest as a CSV — all signal dates from the DB."""
    import io
    import psycopg2
    from psycopg2.extras import RealDictCursor

    settings = get_settings()
    if not settings.supabase_conn_str:
        return jsonify({"error": "SUPABASE_CONN_STR is missing."}), 500

    start = parse_date(request.args.get("start")) or date(2024, 1, 1)
    end   = parse_date(request.args.get("end"))   or date.today()

    try:
        with psycopg2.connect(settings.supabase_conn_str) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        p.signal_date,
                        p.next_trade_date            AS trade_date,
                        p.regime,
                        COALESCE(p.effective_prediction, p.final_prediction) AS effective_prediction,
                        p.final_prediction,
                        p.promoted_prediction,
                        p.promotion_reason,
                        p.primary_strategy,
                        p.primary_strategy_family,
                        p.primary_strategy_type,
                        p.watch_variant,
                        p.watch_family,
                        p.watch_strategy_type,
                        p.prior_watch_variant,
                        p.prior_watch_family,
                        p.prior_watch_strategy_type,
                        p.confirming_variant,
                        p.confirming_family,
                        p.family_confirmation_match,
                        p.promotion_block_reason,
                        p.event_gate_reason,
                        p.watch_signal,
                        p.prior_watch_signal,
                        p.prior_watch_age,
                        p.actual_trade_label,
                        p.actual_quality_label,
                        p.strength_score,
                        p.confidence_level,
                        p.global_gate_reason,
                        p.global_us_return_mean,
                        p.global_europe_return_mean,
                        p.global_asia_return_mean,
                        p.vix_close,
                        p.regime
                    FROM "NiftyPrediction" p
                    WHERE p.symbol = %(symbol)s
                      AND p.model_version = %(model_version)s
                      AND p.signal_date BETWEEN %(start)s AND %(end)s
                    ORDER BY p.signal_date
                    """,
                    {"symbol": NIFTY_SYMBOL, "model_version": MODEL_VERSION,
                     "start": start, "end": end},
                )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if not rows:
        return jsonify({"error": "No production rows found for the given date range."}), 404

    df = pd.DataFrame(rows)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    filename = f"production_backtest_{start.isoformat()}_to_{end.isoformat()}.csv"
    return send_file(
        io.BytesIO(buf.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


@app.post("/production/analyze-misses")
def production_analyze_misses():
    """Generate the production precision/recall miss reports on demand."""
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "scripts/Common/analyze_precision_misses.py"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Miss analysis timed out after 10 minutes."}), 504
    except OSError as exc:
        return jsonify({"error": f"Could not start miss analysis: {exc}"}), 500
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "Miss analysis failed.").strip()[-1200:]
        return jsonify({"error": error}), 500

    missing = [path.name for path in MISS_ANALYSIS_FILES.values() if not path.exists()]
    if missing:
        return jsonify({"error": "Analysis completed without creating: " + ", ".join(missing)}), 500

    return jsonify({
        "message": "Miss analysis complete.",
        "downloads": [
            url_for("production_miss_analysis_file", report=report)
            for report in ("precision", "recall")
        ],
    })


@app.get("/production/analyze-misses/download/<report>")
def production_miss_analysis_file(report: str):
    path = MISS_ANALYSIS_FILES.get(report)
    if path is None or not path.exists():
        abort(404)
    return send_file(path.resolve(), as_attachment=True, download_name=path.name)


@app.get("/research/output/<name>")
def research_output_file(name: str):
    filename = RESEARCH_OUTPUT_FILES.get(name)
    if not filename:
        abort(404)
    path = RESEARCH_OUTPUT_DIR / filename
    if not path.exists():
        abort(404)
    return send_file(path.resolve(), as_attachment=False)


@app.route("/research/run", methods=["POST"])
def research_run():
    """Start research grid as a background job. Returns {job_id} JSON."""
    try:
        from backtest.vectorbt_research.strategy_grid import DEFAULT_VARIANTS, run_strategy_grid

        selected_variant_names = request.form.getlist("variants")
        variants = None
        if selected_variant_names and "__ALL__" not in selected_variant_names:
            variants = [v for v in DEFAULT_VARIANTS if v.name in selected_variant_names]
            if not variants:
                return jsonify({"error": "No strategy variants selected."}), 400

        start = parse_date(request.form.get("start")) or RESEARCH_DEFAULT_START
        end = parse_date(request.form.get("end")) or date.today()
        target_pcts = parse_float_values(request.form.getlist("target_pct"), 0.05)
        stop_loss_pcts = parse_optional_float_values(request.form.getlist("stop_loss_pct"))

        job_id = str(uuid.uuid4())
        with _RESEARCH_JOBS_LOCK:
            _RESEARCH_JOBS[job_id] = {"state": "running", "message": "", "error": "",
                                       "started_at": datetime.now().isoformat()}

        def _run():
            try:
                paths = run_strategy_grid(
                    start=start, end=end,
                    target_pcts=target_pcts, stop_loss_pcts=stop_loss_pcts,
                    output_dir=RESEARCH_OUTPUT_DIR, variants=variants,
                )
                msg = "Research grid completed. Outputs: " + ", ".join(paths.keys())
                with _RESEARCH_JOBS_LOCK:
                    _RESEARCH_JOBS[job_id].update({"state": "done", "message": msg})
            except Exception as exc:
                with _RESEARCH_JOBS_LOCK:
                    _RESEARCH_JOBS[job_id].update({"state": "failed", "error": str(exc)})

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"job_id": job_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/research/status/<job_id>")
def research_status(job_id: str):
    """Poll job state. Returns {state, message, error, started_at}."""
    with _RESEARCH_JOBS_LOCK:
        job = _RESEARCH_JOBS.get(job_id)
    if job is None:
        return jsonify({"state": "unknown"}), 404
    return jsonify(job)


def load_strategy_definition_map() -> dict[str, str]:
    """Load strategy variant definitions for UI hover tooltips."""
    definitions: dict[str, str] = {}
    try:
        from backtest.vectorbt_research.strategy_grid import DEFAULT_VARIANTS
        from src.technical_analysis.strategy_families import get_strategy_family_registry

        registry = get_strategy_family_registry()
        current_variants = {variant.name for variant in DEFAULT_VARIANTS}
    except Exception:
        registry = None
        current_variants = set()

    def _regime_for_family(family: str) -> str:
        if registry is None:
            return "ALL"
        regime = str((registry.families.get(family, {}) or {}).get("regime", "all"))
        return regime.upper()

    def _guard_note(name: str, direction: str) -> str:
        if name.endswith("_GlobalAllDisagree"):
            if direction == "PUT":
                return "Variant guard: GlobalAllDisagree - suppresses the PUT when US, Europe, and Asia are all positive."
            if direction == "CALL":
                return "Variant guard: GlobalAllDisagree - suppresses the CALL when US, Europe, and Asia are all negative."
            return "Variant guard: GlobalAllDisagree - suppresses the signal when all global regions disagree with its side."
        if name.endswith("_GlobalAsiaDisagree"):
            if direction == "PUT":
                return "Variant guard: GlobalAsiaDisagree - suppresses the PUT when Asia is positive."
            if direction == "CALL":
                return "Variant guard: GlobalAsiaDisagree - suppresses the CALL when Asia is negative."
            return "Variant guard: GlobalAsiaDisagree - suppresses the signal when Asia disagrees with its side."
        if name.endswith("_GlobalAsiaAgree"):
            return "Variant guard: GlobalAsiaAgree - requires Asia to agree with the signal side."
        return ""

    def _tooltip_for(name: str, fallback_definition: str = "", fallback_family: str = "") -> str:
        if registry is not None:
            try:
                meta = registry.get_meta(name)
                parts = [
                    f"Regime: {_regime_for_family(meta.family)}",
                    f"Family: {meta.family}",
                    f"Type: {meta.strategy_type}",
                    f"Direction: {meta.direction}",
                ]
                guard_note = _guard_note(name, meta.direction)
                if guard_note:
                    parts.append(guard_note)
                definition = meta.definition or fallback_definition
                if definition:
                    parts.append(definition)
                return "\n".join(parts)
            except KeyError:
                pass
        parts = [f"Regime: {_regime_for_family(fallback_family)}"]
        if fallback_family:
            parts.append(f"Family: {fallback_family}")
        if fallback_definition:
            parts.append(fallback_definition)
        return "\n".join(parts)

    if registry is not None:
        for name in sorted(current_variants):
            definitions[name] = _tooltip_for(name)

    for path in STRATEGY_DEFINITION_PATHS:
        if not path.exists():
            continue
        try:
            defs_df = pd.read_csv(path)
        except Exception:
            continue

        if "name" in defs_df.columns:
            name_col = "name"
        elif "strategy_variant" in defs_df.columns:
            name_col = "strategy_variant"
        else:
            continue

        for _, row in defs_df.iterrows():
            name = str(row.get(name_col) or "").strip()
            if not name or name.lower() == "nan":
                continue
            # Generated research definition artifacts may lag behind the code.
            # Only keep their rows when the variant still exists today.
            if current_variants and name not in current_variants and path in STRATEGY_DEFINITION_PATHS:
                continue
            definition = str(row.get("definition") or "").strip() if "definition" in defs_df.columns else ""
            if not definition or definition.lower() == "nan":
                other_cols = [c for c in defs_df.columns if c != name_col]
                parts = [
                    f"{c}: {row[c]}"
                    for c in other_cols
                    if pd.notna(row[c]) and str(row[c]).strip()
                ]
                definition = "\n".join(parts)
            if definition:
                family = str(row.get("family") or row.get("strategy_family") or "").strip()
                definitions[name] = _tooltip_for(name, definition, family)
    return definitions


@app.route("/research", methods=["GET", "POST"])
def research():
    # POST is kept for non-JS fallback but simply redirects — JS path uses /research/run
    message = request.args.get("message", "")
    error = request.args.get("error", "")

    leaderboard_path = RESEARCH_OUTPUT_DIR / "strategy_grid_leaderboard.csv"

    if leaderboard_path.exists():
        try:
            lb_df = pd.read_csv(leaderboard_path).head(200)
            from backtest.vectorbt_research.strategy_grid import DEFAULT_VARIANTS
            from src.technical_analysis.strategy_families import get_strategy_family_registry

            registry = get_strategy_family_registry()
            current_variants = {variant.name for variant in DEFAULT_VARIANTS}
            lb_df = lb_df[lb_df["strategy_variant"].astype(str).isin(current_variants)].copy()

            # Always overlay canonical metadata so stale generated CSVs cannot
            # display removed variants or old production/research tags.
            def _meta_value(value: object, attr: str, default: str) -> str:
                try:
                    return getattr(registry.get_meta(str(value)), attr)
                except KeyError:
                    return default

            lb_df["strategy_family"] = lb_df["strategy_variant"].map(
                lambda value: _meta_value(value, "family", "Unknown")
            )
            lb_df["strategy_type"] = lb_df["strategy_variant"].map(
                lambda value: _meta_value(value, "strategy_type", "UNKNOWN")
            )

            # Keep the new attribution contract visible for legacy leaderboard
            # CSVs. A fresh research run replaces these blanks with real metrics.
            for column in (
                "watch_promotions",
                "watch_promotion_precision",
                "watch_promotion_recall",
            ):
                if column not in lb_df.columns:
                    lb_df[column] = pd.NA
            if "fires" not in lb_df.columns and {"call_fires", "put_fires"}.issubset(lb_df.columns):
                lb_df["fires"] = (
                    pd.to_numeric(lb_df["call_fires"], errors="coerce").fillna(0)
                    + pd.to_numeric(lb_df["put_fires"], errors="coerce").fillna(0)
                ).astype(int)

            # Family is the visual/sort group; variants in one family share colour.
            lb_df["_group"] = lb_df["strategy_family"].fillna("Unknown").astype(str)
            lb_df["_wr_sort"] = pd.to_numeric(lb_df.get("win_rate_pct"), errors="coerce").fillna(-1)
            group_rank = lb_df.groupby("_group")["_wr_sort"].max().rename("_group_rank")
            lb_df = lb_df.merge(group_rank, on="_group", how="left")
            # Sort: groups ordered by their best win_rate desc, rows within group also by win_rate desc
            lb_df = lb_df.sort_values(["_group_rank", "_wr_sort"], ascending=[False, False])
            ordered_groups = list(dict.fromkeys(lb_df["_group"].tolist()))
            group_color_idx = {g: i for i, g in enumerate(ordered_groups)}

            # Build {variant_name: color_index} for JS row coloring.
            group_colors_map: dict[str, int] = {
                str(row["strategy_variant"]): group_color_idx.get(str(row["_group"]), 0)
                for _, row in lb_df.iterrows()
            }
            lb_df = lb_df.drop(columns=["_group", "_wr_sort", "_group_rank"])

            # Signal-quality fields remain in the leaderboard CSV/calculation but are
            # intentionally hidden from the UI for now.
            _LB_DISPLAY_COLS = [
                "strategy_variant", "strategy_family", "strategy_type", "target_pct",
                "trades", "fires",
                "direction_win_rate_pct",
                "actualTradeLabel_precision", "actualTradeLabel_recall", "actualTradeLabel_F1",
                "qualityBased_precision", "qualityBased_recall", "qualityBased_F1",
                "wins", "losses", "win_rate_pct",
                "total_pnl_per_lot", "avg_pnl_per_unit",
            ]
            lb_df = lb_df[[c for c in _LB_DISPLAY_COLS if c in lb_df.columns]]
            lb_df = lb_df.rename(columns={
                "actualTradeLabel_precision": "precision",
                "actualTradeLabel_recall": "recall",
                "actualTradeLabel_F1": "F1",
            })

            lb_html = lb_df.to_html(index=False, classes="data-table sortable-table", border=0, escape=True)
            for strategy_type, description in STRATEGY_TYPE_TOOLTIPS.items():
                lb_html = lb_html.replace(
                    f"<td>{strategy_type}</td>",
                    f'<td title="{html.escape(description, quote=True)}">{strategy_type}</td>',
                )
            lb_html = lb_html.replace(
                'class="dataframe data-table sortable-table"',
                'id="leaderboard-table" class="dataframe data-table sortable-table"',
                1,
            )
            scripts = f'<script>window._STRATEGY_GROUP_COLORS={_json_mod.dumps(group_colors_map)};</script>'
            lb_html = scripts + lb_html
            leaderboard = PageTable(
                "Strategy Leaderboard", leaderboard_path, lb_html, len(lb_df),
            )
        except Exception as exc:
            leaderboard = PageTable("Strategy Leaderboard", leaderboard_path, "", 0,
                                    empty_message=f"Could not read CSV: {exc}")
    else:
        leaderboard = PageTable("Strategy Leaderboard", leaderboard_path, "", 0)
    predictions = research_predictions_table(
        RESEARCH_OUTPUT_DIR / "strategy_grid_predictions.csv",
    )
    trades = research_artifact_table(
        "Research Trades",
        RESEARCH_OUTPUT_DIR / "strategy_grid_trades.csv",
        table_id="research-trades-table",
    )
    summary = read_text(RESEARCH_OUTPUT_DIR / "strategy_grid_summary.txt")
    if not message and not error:
        message = research_output_message(RESEARCH_OUTPUT_DIR)

    return render_dashboard(
        active="research",
        message=message,
        error=error,
        title="Research",
        subtitle="Run VectorBT across research strategy variants and compare PnL, win rate, and generated option trades.",
        controls=research_controls(),
        tables=[leaderboard, predictions, trades],
        summary="",
        summary_title="",
    )


def research_output_message(output_dir: Path) -> str:
    files = [
        output_dir / "strategy_grid_leaderboard.csv",
        output_dir / "strategy_grid_predictions.csv",
        output_dir / "strategy_grid_definitions.csv",
        output_dir / "strategy_grid_trades.csv",
        output_dir / "strategy_grid_watch_promotions.csv",
        output_dir / "strategy_grid_summary.txt",
    ]
    existing = [path for path in files if path.exists()]
    if not existing:
        return ""
    latest = max(path.stat().st_mtime for path in existing)
    refreshed_at = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M:%S")
    return f"Showing existing research outputs from {output_dir} refreshed at {refreshed_at}."


def research_predictions_table(path: Path, limit: int | None = None) -> PageTable:
    if not path.exists():
        return PageTable(title="Research Prediction", path=path, html="", rows=0)
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        return PageTable(
            title="Research Prediction",
            path=path,
            html="",
            rows=0,
            empty_message=f"Could not read CSV: {exc}",
        )
    if df.empty:
        return PageTable(title="Research Prediction", path=path, html="", rows=0)

    display_cols = [
        "signal_date",
        "trade_date",
        "strategy_variant",
        "strategy_family",
        "strategy_type",
        "regime",
        "predicted",
        "actual_label",
        "quality_label",
        "us_ret",
        "europe_ret",
        "asia_ret",
    ]
    display = _add_research_strategy_metadata(df)
    display = display[[c for c in display_cols if c in display.columns]].copy()
    if "signal_date" in display.columns:
        display["signal_date"] = display["signal_date"].astype(str)
        display = display.sort_values(["signal_date", "strategy_variant"], ascending=[False, True])
    for col in ("us_ret", "europe_ret", "asia_ret"):
        if col in display.columns:
            numeric = pd.to_numeric(display[col], errors="coerce")
            display[col] = numeric.map(lambda value: f"{value:.2%}" if pd.notna(value) else "")

    visible = display.head(limit) if limit is not None else display
    html_str = visible.to_html(
        index=False,
        classes="data-table sortable-table",
        border=0,
        escape=True,
    )
    html_str = html_str.replace(
        'class="dataframe data-table sortable-table"',
        'id="research-predictions-table" class="dataframe data-table sortable-table"',
        1,
    )
    return PageTable(
        title="Research Prediction",
        path=path,
        html=html_str,
        rows=len(display),
    )


def research_artifact_table(
    title: str,
    path: Path,
    *,
    table_id: str,
    limit: int | None = None,
) -> PageTable:
    if not path.exists():
        return PageTable(title=title, path=path, html="", rows=0)
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        return PageTable(title=title, path=path, html="", rows=0, empty_message=f"Could not read CSV: {exc}")
    if df.empty:
        return PageTable(title=title, path=path, html="", rows=0)

    display = _add_research_strategy_metadata(df)
    for col in ("signal_date", "trade_date", "replay_trade_date", "entry_time", "exit_time", "snapshot_time"):
        if col in display.columns:
            display[col] = display[col].astype(str)
    visible = display.head(limit) if limit is not None else display
    html_str = visible.to_html(
        index=False,
        classes="data-table sortable-table",
        border=0,
        escape=True,
    )
    html_str = html_str.replace(
        'class="dataframe data-table sortable-table"',
        f'id="{table_id}" class="dataframe data-table sortable-table"',
        1,
    )
    return PageTable(title=title, path=path, html=html_str, rows=len(display))


def _add_research_strategy_metadata(df: pd.DataFrame) -> pd.DataFrame:
    if "strategy_variant" not in df.columns:
        return df.copy()

    from src.technical_analysis.strategy_families import get_strategy_family_registry

    registry = get_strategy_family_registry()
    out = df.copy()

    def _meta_value(value: object, attr: str, default: str) -> str:
        try:
            return getattr(registry.get_meta(str(value)), attr)
        except KeyError:
            return default

    family_values = out["strategy_variant"].map(lambda value: _meta_value(value, "family", "Unknown"))
    type_values = out["strategy_variant"].map(lambda value: _meta_value(value, "strategy_type", "UNKNOWN"))
    if "strategy_family" not in out.columns:
        out.insert(min(1, len(out.columns)), "strategy_family", family_values)
    else:
        out["strategy_family"] = family_values
    if "strategy_type" not in out.columns:
        out.insert(min(2, len(out.columns)), "strategy_type", type_values)
    else:
        out["strategy_type"] = type_values
    return out


PRODUCTION_DEFAULT_START = date(2024, 1, 1)


def production_default_end() -> date:
    """Resolve per request so a long-running UI server does not retain yesterday's date."""
    return date.today()


def _fmt_optional_metric(value: float | int | None) -> str:
    return f"{float(value):.3f}" if value is not None and pd.notna(value) else "n/a"


def build_promoted_roster_table() -> PageTable:
    """Build a production hard-trade strategy table with audit metrics."""
    try:
        from src.technical_analysis.cascade.dataset import build_base, regime_frame
        from src.technical_analysis.cascade.engine import _side_precisions
        from src.technical_analysis.cascade.strategies import ALL_PARTICIPATING_REGIME_FAMILIES
        from src.technical_analysis.cascade.constants import (
            REGIME_PRECISION_FLOOR, MIN_FIRES, REGIME_STRESS, REGIME_CALM,
            PRODUCTION_BACKTEST_START,
        )
        from src.technical_analysis.cascade.dataset import _call_ok, _put_ok
        from src.technical_analysis.cascade.constants import CALL, PUT
        from src.technical_analysis.prediction.signal_strength import (
            add_raw_direction, quality_label_metrics, summarize_signal_quality,
        )

        resolved = add_raw_direction(build_base())
        resolved = resolved[
            (pd.to_datetime(resolved["signal_date"]) >= pd.Timestamp(PRODUCTION_BACKTEST_START))
            & resolved["next_open"].notna()
        ].reset_index(drop=True)
        rows = []
        for regime in [REGIME_STRESS, REGIME_CALM]:
            floor = REGIME_PRECISION_FLOOR[regime]
            families = ALL_PARTICIPATING_REGIME_FAMILIES[regime]
            elig_df = regime_frame(resolved, regime)
            call_ok = _call_ok(elig_df)
            put_ok = _put_ok(elig_df)
            n_call_opps = int(call_ok.sum())
            n_put_opps = int(put_ok.sum())
            signals: dict = {}
            for fn in families.values():
                for col, sig in fn(resolved).items():
                    name = col.replace("strategy_", "").replace("_signal", "")
                    signals[name] = sig

            prec = _side_precisions(elig_df, signals)
            for name, (cp, nc, pp, npp) in sorted(prec.items()):
                # CALL side
                if nc > 0:
                    call_quality = summarize_signal_quality(
                        signals[name].where(signals[name] == CALL, "NO_POSITION"), elig_df
                    )
                    call_quality_label = quality_label_metrics(
                        signals[name], elig_df["actual_quality_label"], side=CALL
                    )
                    correct_c = round(cp * nc) if cp == cp else 0
                    call_recall = correct_c / n_call_opps if n_call_opps else float("nan")
                    call_f1 = (2 * cp * call_recall / (cp + call_recall)
                               if cp == cp and call_recall == call_recall and (cp + call_recall) > 0
                               else float("nan"))
                    call_elig = nc >= MIN_FIRES and cp == cp and cp > floor
                    rows.append({
                        "regime": regime,
                        "strategy": name,
                        "side": "CALL",
                        "fires": nc,
                        "precision": f"{cp:.3f}" if cp == cp else "n/a",
                        "recall": f"{call_recall:.3f}" if call_recall == call_recall else "n/a",
                        "F1": f"{call_f1:.3f}" if call_f1 == call_f1 else "n/a",
                        "qualityBased_precision": _fmt_optional_metric(call_quality_label["qualityBased_precision"]),
                        "qualityBased_recall": _fmt_optional_metric(call_quality_label["qualityBased_recall"]),
                        "qualityBased_F1": _fmt_optional_metric(call_quality_label["qualityBased_F1"]),
                        **call_quality,
                        "historical_floor_pass": "YES" if call_elig else "-",
                    })
                # PUT side
                if npp > 0:
                    put_quality = summarize_signal_quality(
                        signals[name].where(signals[name] == PUT, "NO_POSITION"), elig_df
                    )
                    put_quality_label = quality_label_metrics(
                        signals[name], elig_df["actual_quality_label"], side=PUT
                    )
                    correct_p = round(pp * npp) if pp == pp else 0
                    put_recall = correct_p / n_put_opps if n_put_opps else float("nan")
                    put_f1 = (2 * pp * put_recall / (pp + put_recall)
                              if pp == pp and put_recall == put_recall and (pp + put_recall) > 0
                              else float("nan"))
                    put_elig = npp >= MIN_FIRES and pp == pp and pp > floor
                    rows.append({
                        "regime": regime,
                        "strategy": name,
                        "side": "PUT",
                        "fires": npp,
                        "precision": f"{pp:.3f}" if pp == pp else "n/a",
                        "recall": f"{put_recall:.3f}" if put_recall == put_recall else "n/a",
                        "F1": f"{put_f1:.3f}" if put_f1 == put_f1 else "n/a",
                        "qualityBased_precision": _fmt_optional_metric(put_quality_label["qualityBased_precision"]),
                        "qualityBased_recall": _fmt_optional_metric(put_quality_label["qualityBased_recall"]),
                        "qualityBased_F1": _fmt_optional_metric(put_quality_label["qualityBased_F1"]),
                        **put_quality,
                        "historical_floor_pass": "YES" if put_elig else "-",
                    })

        df = pd.DataFrame(rows, columns=[
            "regime", "strategy", "side", "fires", "precision", "recall", "F1",
            "qualityBased_precision", "qualityBased_recall", "qualityBased_F1",
            "quality_scored_fires", "mean_signal_quality", "median_signal_quality",
            "positive_quality_rate_pct", "historical_floor_pass",
        ])
        # Sort by F1 descending
        df = df.iloc[sorted(range(len(df)), key=lambda i: -float(df.iloc[i]["F1"]) if df.iloc[i]["F1"] not in ("n/a", "") else -1.0)]
        unique_strategies = int(df["strategy"].nunique()) if not df.empty else 0
        # Preserve quality computation/data above; hide only the rendered columns.
        display_df = df.drop(columns=[
            "quality_scored_fires", "mean_signal_quality", "median_signal_quality",
            "positive_quality_rate_pct", "historical_floor_pass",
        ], errors="ignore")
        html_str = display_df.to_html(
            index=False, classes="data-table sortable-table", border=0, escape=True
        )
        return PageTable(
            title=(f"Production Strategies — {unique_strategies} unique strategies, "
                   f"{len(df)} strategy-side rows"),
            path=None,
            html=html_str,
            rows=len(display_df),
        )
    except Exception as exc:
        return PageTable(
            title="Production Strategies",
            path=None,
            html="",
            rows=0,
            empty_message=f"Could not build roster table: {exc}",
        )


@app.post("/production/recompute")
def production_recompute():
    """Start the date-scoped 3-step production pipeline in a background thread.
    Rejects if already running. Returns {state} JSON."""
    import subprocess, sys
    payload = request.get_json(silent=True) or request.form or {}
    start = parse_date(payload.get("start")) or PRODUCTION_DEFAULT_START
    end = parse_date(payload.get("end")) or production_default_end()
    if end < start:
        return jsonify({"state": "error", "error": "End date must be on or after start date."}), 400

    with _RECOMPUTE_LOCK:
        if _RECOMPUTE_JOB["state"] == "running":
            return jsonify({"state": "running", "error": "Pipeline already running."}), 409
        _RECOMPUTE_JOB.update({"state": "running", "message": "", "error": "",
                                "started_at": datetime.now().isoformat(),
                                "start": start.isoformat(), "end": end.isoformat()})

    def _run():
        start_arg = start.isoformat()
        end_arg = end.isoformat()
        steps = [
            [sys.executable, "scripts/daily_NIFTY/daily_nifty_prediction.py",
             "--start", start_arg, "--end", end_arg],
            [sys.executable, "backtest/production/pipeline_upsert_option_selections.py",
             "--start", start_arg, "--end", end_arg],
            [sys.executable, "backtest/production/pipeline_backtest_pnl.py",
             "--start", start_arg, "--end", end_arg],
        ]
        for cmd in steps:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "Unknown error").strip()[-800:]
                with _RECOMPUTE_LOCK:
                    _RECOMPUTE_JOB.update({"state": "error", "error": f"{cmd[1]}: {err}"})
                return
        with _RECOMPUTE_LOCK:
            _RECOMPUTE_JOB.update({
                "state": "done",
                "message": f"Predictions regenerated, option selections upserted, PnL backtest complete for {start_arg} to {end_arg}.",
            })

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"state": "running"})


@app.get("/production/recompute-status")
def production_recompute_status():
    with _RECOMPUTE_LOCK:
        return jsonify(dict(_RECOMPUTE_JOB))


def build_drift_prediction_metrics(start: date, end: date) -> str:
    """Compute precision / recall of drift_effective_prediction vs actual_trade_label
    for all rows in the date range where the drift overrule was computed.
    Returns a formatted text block (empty string if no data or DB unavailable).
    """
    settings = get_settings()
    if not settings.supabase_conn_str:
        return ""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        with psycopg2.connect(settings.supabase_conn_str) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT drift_effective_prediction, drift_overrule_reason,
                           actual_trade_label, actual_quality_label
                    FROM "NiftyPrediction"
                    WHERE symbol = 'NIFTY' AND model_version = 'cascade_v1'
                      AND signal_date BETWEEN %s AND %s
                      AND drift_effective_prediction IS NOT NULL
                    """,
                    (start, end),
                )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        return f"(drift metrics unavailable: {exc})"

    if not rows:
        return "(no drift overrule rows in selected date range)"

    total = len(rows)
    fires = [r for r in rows if r["drift_effective_prediction"] in ("CALL", "PUT")]
    n_fires = len(fires)
    # Precision: correct fires / total fires
    correct = sum(
        1 for r in fires
        if r["actual_trade_label"] in (r["drift_effective_prediction"], "BOTH")
    )
    precision = correct / n_fires if n_fires else None
    # Recall: correct fires / total actual moves that happened
    actual_moves = [r for r in rows if r["actual_trade_label"] in ("CALL", "PUT", "BOTH")]
    # Recall = correct / total graded rows with an actual move
    recall = correct / len(actual_moves) if actual_moves else None
    # Quality-based precision
    q_correct = sum(
        1 for r in fires
        if r["actual_quality_label"] in (r["drift_effective_prediction"], "BOTH")
        and r["actual_quality_label"] is not None
    )
    q_graded = [r for r in fires if r["actual_quality_label"] is not None]
    q_prec = q_correct / len(q_graded) if q_graded else None
    # Breakdown by reason
    from collections import Counter
    reason_counts = Counter(r["drift_overrule_reason"] for r in rows)
    # Drift-promoted (net-new trades from NO_POSITION)
    promo_reasons = {"DRIFT_PROBE", "TAIL_SHOCK", "DRIFT_PROMOTES_WATCH"}
    promo_fires = [r for r in fires if r["drift_overrule_reason"] in promo_reasons]
    promo_correct = sum(
        1 for r in promo_fires
        if r["actual_trade_label"] in (r["drift_effective_prediction"], "BOTH")
    )
    promo_prec = promo_correct / len(promo_fires) if promo_fires else None

    lines = [
        "Drift Overrule — Precision & Recall",
        "-" * 62,
        f"  total rows      : {total}  (drift computed)",
        f"  drift fires     : {n_fires} (CALL {sum(1 for r in fires if r['drift_effective_prediction']=='CALL')}, "
        f"PUT {sum(1 for r in fires if r['drift_effective_prediction']=='PUT')})",
        f"  precision       : {f'{precision:.3f}' if precision is not None else 'n/a'}  "
        f"({'correct fires'}/{n_fires} fires)",
        f"  recall          : {f'{recall:.3f}' if recall is not None else 'n/a'}  "
        f"(against {len(actual_moves)} actual moves)",
        f"  quality prec    : {f'{q_prec:.3f}' if q_prec is not None else 'n/a'}  "
        f"(quality-graded fires: {len(q_graded)})",
        "",
        "  Drift-promoted net-new trades (DRIFT_PROBE + TAIL_SHOCK + WATCH_PROMOTE):",
        f"    fires={len(promo_fires)}  precision={f'{promo_prec:.3f}' if promo_prec is not None else 'n/a'}",
        "",
        "  Reason breakdown:",
    ]
    for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
        lines.append(f"    {reason:<35} {cnt:>4}")
    return "\n".join(lines)


def build_production_signal_table(start: date, end: date, predicted_filter: str) -> tuple[PageTable, str]:
    db_rows, db_error = load_production_signal_rows(start, end)
    if predicted_filter:
        db_rows = [r for r in db_rows if r.get("predicted", "") == predicted_filter]
    raw_html = df_to_html(pd.DataFrame(db_rows))
    return PageTable(
        title="Daily Prediction & Option Selection",
        path=None,
        html=_inject_th_tooltips(raw_html, PRODUCTION_COLUMN_TOOLTIPS),
        rows=len(db_rows),
        empty_message="No rows found for the selected date range.",
        controls_html=production_controls(start, end, predicted_filter),
        section_id="production-signal-table-card",
    ), db_error


@app.get("/production/table")
def production_table_fragment():
    start = parse_date(request.args.get("start")) or PRODUCTION_DEFAULT_START
    end = parse_date(request.args.get("end")) or production_default_end()
    predicted_filter = request.args.get("predicted", "")
    table, error = build_production_signal_table(start, end, predicted_filter)
    return render_table_card(table, error=error)


@app.get("/production")
def production():
    error = ""
    start = parse_date(request.args.get("start")) or PRODUCTION_DEFAULT_START
    end = parse_date(request.args.get("end")) or production_default_end()
    predicted_filter = request.args.get("predicted", "")

    db_table, db_error = build_production_signal_table(start, end, predicted_filter)
    if db_error:
        error = db_error

    global_rows, global_error = load_global_index_window_rows()
    if global_error and not error:
        error = global_error

    # Build chart JSON: {index_code: [{date, return_pct, open, high, low, close}, ...]}
    import json as _json
    from collections import defaultdict as _dd
    _chart: dict = _dd(list)
    for r in global_rows:
        _chart[r["index_code"]].append({
            "date": str(r["trade_date"]),
            "y": float(r["return_pct"]) if r["return_pct"] is not None else None,
            "o": float(r["open_price"]) if r["open_price"] is not None else None,
            "h": float(r["high_price"]) if r["high_price"] is not None else None,
            "l": float(r["low_price"]) if r["low_price"] is not None else None,
            "c": float(r["close_price"]) if r["close_price"] is not None else None,
        })
    for idx in _chart:
        _chart[idx].sort(key=lambda p: p["date"])
    global_indices_json = _json.dumps(dict(_chart))
    global_indices_count = len(_chart)

    roster_table = build_promoted_roster_table()

    return render_dashboard(
        active="production",
        error=error,
        title="Production Strategies",
        subtitle="Production daily direction predictions joined with option selection, entry, target and P&L status.",
        controls="",
        tables=[roster_table, db_table],
        summary=(
            read_text(PRODUCTION_OUTPUT_DIR / "NIFTY_prediction_summary.txt")
            + "\n\n"
            + build_drift_prediction_metrics(start, end)
        ),
        summary_title="Prediction Accuracy & Recall Summary",
        global_indices_json=global_indices_json,
        global_indices_rows=global_indices_count,
    )


@app.route("/trades", methods=["GET", "POST"])
def trades():
    message, error = "", ""
    trade_date = (
        parse_date(request.values.get("trade_date"))
        or load_latest_option_next_trade_date()
        or date.today()
    )
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "prepare":
                from src.execution.paper import prepare_paper_signals

                inserted = prepare_paper_signals(trade_date=trade_date, symbol=NIFTY_SYMBOL, model_version=MODEL_VERSION)
                message = f"Prepared paper execution signals for {trade_date}: created or refreshed {inserted} row(s)."
            elif action == "vectorbt_replay":
                from backtest.vectorbt_trades.schemas import StockieVectorBTRequest
                from backtest.vectorbt_trades.service import run_stockie_vectorbt_backtest

                result = run_stockie_vectorbt_backtest(
                    StockieVectorBTRequest(
                        underlying=NIFTY_SYMBOL,
                        model_version=MODEL_VERSION,
                        mode="paper",
                        start_date=parse_date(request.form.get("start")),
                        end_date=parse_date(request.form.get("end")),
                        output_dir=TRADES_OUTPUT_DIR,
                    )
                )
                message = (
                    f"Paper trade VectorBT replay completed with "
                    f"{len(result.trade_plans)} loaded trade(s), {len(result.trades)} closed replay trade(s)."
                )
            else:
                raise ValueError("Unknown trade action.")
        except Exception as exc:
            error = f"Trade action failed: {exc}"

    executed_df, execution_error = load_live_executed_trades()
    if execution_error and not error:
        error = execution_error
    closed_df = _paper_trades_with_status(executed_df, "CLOSED")
    open_df = _paper_trades_with_status(executed_df, "OPEN")
    executed = dataframe_table(
        "Executed Paper Trades", executed_df, limit=200,
        empty_message="No executed paper trades found in the database.",
        timezone="Asia/Kolkata",
    )
    closed = dataframe_table(
        "Closed Paper Trades", closed_df, limit=200,
        empty_message="No closed paper trades found in the database.",
        timezone="Asia/Kolkata",
    )
    open_trades = dataframe_table(
        "Open Paper Trades", open_df, limit=200,
        empty_message="No open paper trades found in the database.",
        timezone="Asia/Kolkata",
    )
    vectorbt_trades = csv_table(
        "VectorBT Trade Replay", TRADES_OUTPUT_DIR / "vectorbt_trades.csv",
        limit=200, timezone="Asia/Kolkata",
    )
    summary = format_trade_summary_times(read_text(TRADES_OUTPUT_DIR / "vectorbt_summary.txt"))

    return render_dashboard(
        active="trades",
        message=message,
        error=error,
        title="Trades",
        subtitle="Prepare paper signals, review live/paper fills, and replay executed trades through VectorBT.",
        controls=trades_controls(),
        tables=[executed, closed, open_trades, vectorbt_trades],
        summary=summary,
        summary_title="Paper Trade Replay Summary",
        trade_date=trade_date.isoformat(),
    )


def render_dashboard(
    active: str,
    title: str,
    subtitle: str,
    controls: str,
    tables: list[PageTable],
    summary: str,
    summary_title: str,
    message: str = "",
    error: str = "",
    trade_date: str = "",
    global_indices_html: str = "",
    global_indices_rows: int = 0,
    global_indices_json: str = "{}",
) -> str:
    controls = controls.replace("{{ trade_date }}", trade_date)
    return render_template_string(
        PAGE_TEMPLATE,
        active=active,
        title=title,
        subtitle=subtitle,
        controls=controls,
        tables=tables,
        summary=summary,
        summary_title=summary_title,
        message=message,
        error=error,
        today=date.today().isoformat(),
        trade_date=trade_date,
        global_indices_html=global_indices_html,
        global_indices_rows=global_indices_rows,
        global_indices_json=global_indices_json,
        strategy_defs_json=_json_mod.dumps(load_strategy_definition_map()),
        render_table_card=render_table_card,
    )


def render_table_card(table: PageTable, error: str = "") -> str:
    return render_template_string(
        TABLE_CARD_TEMPLATE,
        table=table,
        error=error,
    )


def csv_table(
    title: str,
    path: Path,
    limit: int = 100,
    timezone: str | None = None,
) -> PageTable:
    if not path.exists():
        return PageTable(title=title, path=path, html="", rows=0)
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        return PageTable(title=title, path=path, html="", rows=0, empty_message=f"Could not read CSV: {exc}")
    return PageTable(
        title=title, path=path,
        html=df_to_html(df.head(limit), timezone=timezone), rows=len(df),
    )


def dataframe_table(
    title: str,
    df: pd.DataFrame,
    limit: int = 100,
    empty_message: str = "No rows available yet.",
    timezone: str | None = None,
) -> PageTable:
    visible = df.tail(limit) if len(df) > limit else df
    return PageTable(
        title=title, path=None,
        html=df_to_html(visible, timezone=timezone), rows=len(df),
        empty_message=empty_message,
    )


def load_live_executed_trades() -> tuple[pd.DataFrame, str]:
    try:
        from backtest.vectorbt_trades.data_adapter import load_paper_executed_trades

        return load_paper_executed_trades(
            underlying=NIFTY_SYMBOL, model_version=MODEL_VERSION, mode="paper",
        ), ""
    except Exception as exc:
        return pd.DataFrame(), f"Could not load executed paper trades from Supabase: {exc}"


def _paper_trades_with_status(df: pd.DataFrame, status: str) -> pd.DataFrame:
    if df.empty or "trade_status" not in df.columns:
        return df.iloc[0:0].copy()
    return df[df["trade_status"] == status].reset_index(drop=True)


def _inject_th_tooltips(html: str, tooltips: dict[str, str]) -> str:
    """Inject data-col-tip attributes onto <th> elements whose text matches a tooltip key."""
    import re
    import html as _html_mod

    def _replace(m: re.Match) -> str:
        pre_attrs = m.group(1)   # any existing attributes on <th>
        text = m.group(2)        # inner text (column name)
        key = text.strip().lower().replace(" ", "_")
        tip = tooltips.get(key)
        if not tip:
            return m.group(0)
        safe_tip = _html_mod.escape(tip, quote=True)
        return f'<th{pre_attrs} data-col-tip="{safe_tip}">{text}'

    return re.sub(r'<th((?:\s[^>]*)?)>([^<]+)', _replace, html)


def df_to_html(df: pd.DataFrame, timezone: str | None = None) -> str:
    if df.empty:
        return ""
    display = prepare_ui_dataframe(df)
    for col in display.columns:
        col_lower = col.lower()
        if timezone and (col_lower.endswith("_time") or "timestamp" in col_lower):
            parsed = pd.to_datetime(display[col], errors="coerce", utc=True)
            converted = parsed.dt.tz_convert(timezone)
            formatted = converted.dt.strftime("%Y-%m-%d %H:%M:%S IST")
            display[col] = formatted.where(parsed.notna(), display[col].astype(str))
        elif "date" in col_lower or "time" in col_lower:
            display[col] = display[col].astype(str)
    return display.to_html(index=False, classes="data-table", border=0, escape=True)


def prepare_ui_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Expose one effective Predicted column and hide promotion audit lineage."""
    display = df.copy()
    columns_by_lower = {str(column).lower(): column for column in display.columns}
    effective_col = columns_by_lower.get("effective_prediction")
    predicted_col = columns_by_lower.get("predicted")

    if effective_col is not None:
        effective = display[effective_col].fillna("NO_POSITION")
        if predicted_col is not None:
            display[predicted_col] = effective
        else:
            display.insert(0, "Predicted", effective)
    elif predicted_col is not None:
        display[predicted_col] = display[predicted_col].fillna("NO_POSITION")

    hidden = {
        column for column in display.columns
        if str(column).lower() in UI_HIDDEN_PREDICTION_AUDIT_COLUMNS
    }
    display = display.drop(columns=list(hidden), errors="ignore")

    # Production rows use a lower-case internal key for filtering; make the UI
    # heading consistently user-facing without changing route/filter behavior.
    rename = {
        column: "Predicted" for column in display.columns
        if str(column).lower() == "predicted"
    }
    return display.rename(columns=rename)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(errors="replace")


def format_trade_summary_times(value: str) -> str:
    pattern = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00")

    def replace(match: re.Match[str]) -> str:
        return pd.Timestamp(match.group(0)).tz_convert("Asia/Kolkata").strftime(
            "%Y-%m-%d %H:%M:%S IST"
        )

    return pattern.sub(replace, value)


def load_paper_trade_rows(trade_date: date) -> tuple[list[dict[str, Any]], str]:
    settings = get_settings()
    if not settings.supabase_conn_str:
        return [], "SUPABASE_CONN_STR is missing. Paper trades can still be viewed from existing CSV outputs."
    try:
        from src.data_manager.db.client_factory import get_database_client

        db = get_database_client(settings)
        db.connect()
        try:
            return db.list_paper_trade_results(
                trade_date=trade_date,
                statuses=("PLANNED", "OPEN", "CLOSED", "FAILED"),
                symbol=NIFTY_SYMBOL,
                model_version=MODEL_VERSION,
            ), ""
        finally:
            db.close()
    except Exception as exc:
        return [], f"Could not load paper trades from Supabase: {exc}"


def load_latest_option_next_trade_date() -> date | None:
    settings = get_settings()
    if not settings.supabase_conn_str:
        return None
    try:
        import psycopg2

        with psycopg2.connect(settings.supabase_conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MAX(next_trade_date)
                    FROM "NiftyOptionSelection"
                    WHERE UPPER(symbol) = %s
                      AND model_version = %s
                      AND next_trade_date IS NOT NULL
                      AND COALESCE(prediction_direction, '') IN ('CALL', 'PUT')
                      AND primary_buy_token IS NOT NULL
                      AND primary_buy_symbol IS NOT NULL
                    """,
                    (NIFTY_SYMBOL, MODEL_VERSION),
                )
                row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def load_production_signal_rows(start_date: date, end_date: date) -> tuple[list[dict[str, Any]], str]:
    settings = get_settings()
    if not settings.supabase_conn_str:
        return [], "SUPABASE_CONN_STR is missing. Production DB rows cannot be loaded."
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        with psycopg2.connect(settings.supabase_conn_str) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    'ALTER TABLE "NiftyPrediction" '
                    'ADD COLUMN IF NOT EXISTS global_gate_reason varchar(50)'
                )
                cur.execute(
                    'ALTER TABLE "NiftyPrediction" '
                    'ADD COLUMN IF NOT EXISTS global_risk_off boolean'
                )
                for _col in ("global_us_return_mean", "global_europe_return_mean", "global_asia_overnight_return_mean"):
                    cur.execute(
                        f'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS {_col} double precision'
                    )
                for ddl in (
                    'ADD COLUMN IF NOT EXISTS watch_signal varchar(20)',
                    'ADD COLUMN IF NOT EXISTS prior_watch_signal varchar(20)',
                    'ADD COLUMN IF NOT EXISTS prior_watch_age smallint',
                    'ADD COLUMN IF NOT EXISTS promoted_prediction varchar(20)',
                    'ADD COLUMN IF NOT EXISTS effective_prediction varchar(20)',
                    'ADD COLUMN IF NOT EXISTS promotion_reason varchar(500)',
                    'ADD COLUMN IF NOT EXISTS primary_strategy_family varchar(80)',
                    'ADD COLUMN IF NOT EXISTS primary_strategy_type varchar(40)',
                    'ADD COLUMN IF NOT EXISTS watch_family varchar(80)',
                    'ADD COLUMN IF NOT EXISTS watch_variant varchar(120)',
                    'ADD COLUMN IF NOT EXISTS watch_strategy_type varchar(40)',
                    'ADD COLUMN IF NOT EXISTS prior_watch_family varchar(80)',
                    'ADD COLUMN IF NOT EXISTS prior_watch_variant varchar(120)',
                    'ADD COLUMN IF NOT EXISTS prior_watch_strategy_type varchar(40)',
                    'ADD COLUMN IF NOT EXISTS confirming_family varchar(80)',
                    'ADD COLUMN IF NOT EXISTS confirming_variant varchar(120)',
                    'ADD COLUMN IF NOT EXISTS confirming_strategy_type varchar(40)',
                    'ADD COLUMN IF NOT EXISTS family_confirmation_match boolean',
                    'ADD COLUMN IF NOT EXISTS promotion_block_reason varchar(120)',
                    'ADD COLUMN IF NOT EXISTS event_gate_reason varchar(80)',
                    'ADD COLUMN IF NOT EXISTS alt_trade_label varchar(20)',
                    'ADD COLUMN IF NOT EXISTS drift_effective_prediction varchar(20)',
                    'ADD COLUMN IF NOT EXISTS drift_position_size_pct double precision',
                    'ADD COLUMN IF NOT EXISTS drift_overrule_reason varchar(120)',
                ):
                    cur.execute(f'ALTER TABLE "NiftyPrediction" {ddl}')
                cur.execute(
                    PRODUCTION_SIGNAL_SQL,
                    {"symbol": NIFTY_SYMBOL, "model_version": MODEL_VERSION,
                     "start_date": start_date, "end_date": end_date,
                     "max_open_days": get_trade_horizon_days()},
                )
                rows = cur.fetchall()
    except Exception as exc:
        return [], f"Could not load production signal rows from Supabase: {exc}"

    return [format_signal_row(dict(row)) for row in rows], ""


def load_global_index_window_rows(days: int = 5) -> tuple[list[dict[str, Any]], str]:
    settings = get_settings()
    if not settings.supabase_conn_str:
        return [], "SUPABASE_CONN_STR is missing. Global index rows cannot be loaded."

    end_date = date.today()
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        with psycopg2.connect(settings.supabase_conn_str) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    WITH display_dates AS (
                        SELECT DISTINCT trade_date
                        FROM "GlobalIndexOhlc"
                        WHERE trade_date <= %(end_date)s
                        ORDER BY trade_date DESC
                        LIMIT %(days)s
                    )
                    SELECT
                        index_code, trade_date,
                        open_price, high_price, low_price, close_price,
                        CASE
                            WHEN open_price IS NULL OR open_price = 0 THEN NULL
                            ELSE ROUND(((close_price - open_price) / open_price * 100)::numeric, 2)
                        END AS return_pct
                    FROM "GlobalIndexOhlc"
                    WHERE trade_date IN (SELECT trade_date FROM display_dates)
                    ORDER BY trade_date ASC, index_code
                    """,
                    {"end_date": end_date, "days": days},
                )
                rows = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        return [], f"Could not load global index rows from Supabase: {exc}"

    return rows, ""


def format_global_index_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": fmt_date(row.get("trade_date")),
        "index_code": row.get("index_code") or "",
        "open": fmt_money(row.get("open_price")),
        "high": fmt_money(row.get("high_price")),
        "low": fmt_money(row.get("low_price")),
        "close": fmt_money(row.get("close_price")),
        "return_pct": fmt_pct(row.get("return_pct")),
    }


def run_production_pnl(start: date | None = None, end: date | None = None) -> dict[str, Any]:
    from backtest.production import pipeline_backtest_pnl as pnl

    signals = pnl._load_production_signals(NIFTY_SYMBOL, MODEL_VERSION, start, end)
    if signals.empty:
        return {"signals": 0, "trades": 0, "summary": "No production signals found."}

    snapshots = pnl._load_snapshot_prices(signals)
    snap_ids = set(snapshots["trade_id"]) if not snapshots.empty else set()
    no_snapshot = signals[~signals["trade_id"].isin(snap_ids)].copy()
    trades = pnl._simulate_exits(signals, snapshots)
    metrics = pnl._compute_metrics(trades)
    paths = pnl._write_outputs(
        PRODUCTION_OUTPUT_DIR,
        signals,
        no_snapshot,
        trades,
        metrics,
        NIFTY_SYMBOL,
        MODEL_VERSION,
        start,
        end,
    )
    return {
        "signals": len(signals),
        "signals_without_snapshots": len(no_snapshot),
        "trades": len(trades),
        "summary": str(paths["summary"]),
    }


PRODUCTION_SIGNAL_SQL = """
WITH june_predictions AS (
    SELECT *
    FROM "NiftyPrediction"
    WHERE symbol = %(symbol)s
      AND model_version = %(model_version)s
      AND signal_date BETWEEN %(start_date)s AND %(end_date)s
), option_rows AS (
    SELECT *
    FROM "NiftyOptionSelection"
    WHERE symbol = %(symbol)s
      AND model_version = %(model_version)s
      AND trade_date BETWEEN %(start_date)s AND %(end_date)s
), paper_entries AS (
    SELECT
        pes.signal_trade_date,
        pes.paper_trade_date,
        ptr.entry_price AS actual_entry_price
    FROM "PaperExecutionSignal" pes
    JOIN "PaperTradeResult" ptr ON ptr.paper_execution_signal_id = pes.id
    WHERE pes.symbol = %(symbol)s
      AND pes.model_version = %(model_version)s
      AND ptr.entry_price IS NOT NULL
), selected AS (
    SELECT
        p.signal_date,
        p.next_trade_date,
        p.final_prediction,
        p.watch_signal,
        p.prior_watch_signal,
        p.prior_watch_age,
        p.promoted_prediction,
        COALESCE(p.effective_prediction, p.final_prediction) AS effective_prediction,
        p.promotion_reason,
        p.primary_strategy_family,
        p.primary_strategy_type,
        p.watch_family,
        p.watch_variant,
        p.watch_strategy_type,
        p.prior_watch_family,
        p.prior_watch_variant,
        p.prior_watch_strategy_type,
        p.confirming_family,
        p.confirming_variant,
        p.confirming_strategy_type,
        p.family_confirmation_match,
        p.promotion_block_reason,
        p.event_gate_reason,
        p.direction,
        p.global_gate_reason,
        p.global_risk_off,
        p.global_us_return_mean,
        p.global_europe_return_mean,
        p.global_asia_overnight_return_mean,
        p.global_asia_partial_return_mean,
        p.drift_effective_prediction,
        p.drift_position_size_pct,
        p.drift_overrule_reason,
        p.actual_trade_label,
        p.actual_quality_label,
        p.next_open,
        p.next_high,
        p.next_low,
        p.regime,
        p.primary_strategy AS prediction_strategy,
        p.strength_score,
        p.confidence_level,
        o.selected_strategy,
        o.primary_buy_symbol,
        o.primary_buy_token,
        o.primary_buy_strike,
        o.primary_buy_expiry,
        o.primary_buy_option_type,
        o.primary_buy_entry_price,
        o.target_1_pct,
        COALESCE(pe.actual_entry_price, o.primary_buy_entry_price) * (1 + o.target_1_pct)
            AS target_1_price,
        o.stop_loss_pct,
        CASE WHEN o.stop_loss_enabled
             THEN COALESCE(pe.actual_entry_price, o.primary_buy_entry_price) * (1 - o.stop_loss_pct)
        END AS stop_loss_price,
        o.no_trade_reason,
        o.selection_score       AS selected_option_score,
        pe.actual_entry_price
    FROM june_predictions p
    LEFT JOIN option_rows o
      ON o.symbol = p.symbol
     AND o.trade_date = p.signal_date
     AND o.model_version = p.model_version
     AND (p.effective_prediction IN ('CALL', 'PUT')
          OR p.drift_effective_prediction IN ('CALL', 'PUT'))
    LEFT JOIN paper_entries pe
      ON pe.signal_trade_date = p.signal_date
     AND pe.paper_trade_date = p.next_trade_date
)
SELECT
    s.*,
    COALESCE(s.actual_entry_price, s.primary_buy_entry_price) AS effective_entry_price,
    CASE
        WHEN s.next_open > 0 AND s.next_high IS NOT NULL
        THEN ROUND(((s.next_high - s.next_open) / s.next_open * 100)::numeric, 2)
    END AS max_underlying_up,
    CASE
        WHEN s.next_low > 0 AND s.next_open IS NOT NULL
        THEN ROUND(((s.next_open - s.next_low) / s.next_low * 100)::numeric, 2)
    END AS max_underlying_down,
    stats.first_snapshot_time,
    stats.last_snapshot_time,
    stats.max_option_price,
    stats.min_option_price,
    stats.latest_option_price,
    stats.snapshot_count,
    stats.pnl_exit_price,
    CASE
        WHEN COALESCE(s.actual_entry_price, s.primary_buy_entry_price) IS NULL
          OR stats.pnl_exit_price IS NULL THEN NULL
        ELSE ROUND(
            ((stats.pnl_exit_price - COALESCE(s.actual_entry_price, s.primary_buy_entry_price))
            / NULLIF(COALESCE(s.actual_entry_price, s.primary_buy_entry_price), 0) * 100)::numeric,
            2
        )
    END AS latest_pnl_pct,
    CASE
        WHEN COALESCE(s.actual_entry_price, s.primary_buy_entry_price) IS NULL
          OR stats.pnl_exit_price IS NULL THEN NULL
        ELSE ROUND((stats.pnl_exit_price - COALESCE(s.actual_entry_price, s.primary_buy_entry_price))::numeric, 2)
    END AS latest_pnl_points,
    CASE
        WHEN s.primary_buy_symbol IS NULL THEN COALESCE(s.no_trade_reason, 'NO_OPTION_SELECTED')
        WHEN COALESCE(stats.snapshot_count, 0) = 0 THEN 'NO_SNAPSHOT_DATA'
        ELSE stats.exit_status
    END AS pnl_status
FROM selected s
LEFT JOIN LATERAL (
    WITH snaps AS (
        SELECT os.snapshot_time, os.last_price
        FROM "OptionSnapshot" os
        JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
        WHERE oi.instrument_token = s.primary_buy_token
          AND os.trade_date IN (
              SELECT tc.calendar_date
              FROM "TradingCalendar" tc
              WHERE tc.exchange = 'NSE'
                AND tc.is_trading_day = true
                AND tc.calendar_date >= s.next_trade_date
                AND tc.calendar_date <= CURRENT_DATE
              ORDER BY tc.calendar_date
              LIMIT %(max_open_days)s
          )
          AND os.last_price IS NOT NULL
    ),
    exit_event AS (
        SELECT snapshot_time, last_price,
               CASE
                   WHEN s.stop_loss_price IS NOT NULL AND last_price <= s.stop_loss_price THEN 'STOP_LOSS_HIT'
                   WHEN s.target_1_price  IS NOT NULL AND last_price >= s.target_1_price  THEN 'TARGET_HIT'
               END AS exit_type
        FROM snaps
        WHERE (s.stop_loss_price IS NOT NULL AND last_price <= s.stop_loss_price)
           OR (s.target_1_price  IS NOT NULL AND last_price >= s.target_1_price)
        ORDER BY snapshot_time
        LIMIT 1
    )
    SELECT
        (SELECT MIN(snapshot_time) FROM snaps)
            AS first_snapshot_time,
        COALESCE((SELECT snapshot_time FROM exit_event),
                 (SELECT MAX(snapshot_time) FROM snaps))
            AS last_snapshot_time,
        (SELECT last_price FROM snaps ORDER BY snapshot_time DESC LIMIT 1)
            AS latest_option_price,
        COALESCE((SELECT last_price FROM exit_event),
                 (SELECT last_price FROM snaps ORDER BY snapshot_time DESC LIMIT 1))
            AS pnl_exit_price,
        COALESCE((SELECT exit_type FROM exit_event),
                 CASE WHEN (SELECT COUNT(*) FROM snaps) > 0 THEN 'OPEN' ELSE 'NO_SNAPSHOT_DATA' END)
            AS exit_status,
        (SELECT COUNT(*) FROM snaps)
            AS snapshot_count,
        (SELECT MAX(last_price) FROM snaps
         WHERE snapshot_time <= COALESCE((SELECT snapshot_time FROM exit_event),
                                         'infinity'::timestamptz))
            AS max_option_price,
        (SELECT MIN(last_price) FROM snaps
         WHERE snapshot_time <= COALESCE((SELECT snapshot_time FROM exit_event),
                                         'infinity'::timestamptz))
            AS min_option_price
) stats ON true
ORDER BY s.signal_date;
"""


def format_signal_row(row: dict[str, Any]) -> dict[str, Any]:
    promoted = row.get("promoted_prediction") in ("CALL", "PUT")
    return {
        "signal_date": fmt_date(row.get("signal_date")),
        "trade_date": fmt_date(row.get("next_trade_date")),
        "predicted": row.get("effective_prediction") or "NO_POSITION",
        "signal_strength": fmt_number(row.get("strength_score")),
        "us_ret": fmt_ret_decimal(row.get("global_us_return_mean")),
        "europe_ret": fmt_ret_decimal(row.get("global_europe_return_mean")),
        "asia_ret": fmt_ret_decimal(row.get("global_asia_return_mean")),  # aliased from overnight
        "asia_partial_ret": fmt_ret_decimal(row.get("global_asia_partial_return_mean")),
        "asia_overnight_ret": fmt_ret_decimal(row.get("global_asia_overnight_return_mean")),
        "actual_label": row.get("actual_trade_label") or "Pending",
        "quality_label": row.get("actual_quality_label") or "",
        "max_underlying_up": fmt_pct(row.get("max_underlying_up")),
        "max_underlying_down": fmt_pct(row.get("max_underlying_down")),
        "regime": row.get("regime") or "",
        "prediction_strategy": row.get("prediction_strategy") or "",
        "watched_strategy": (row.get("prior_watch_variant") if promoted else row.get("watch_variant")) or "",
        "option_selection": row.get("selected_strategy") or row.get("no_trade_reason") or "No selection",
        "selected_option_score": fmt_number(row.get("selected_option_score")),
        "option_symbol": row.get("primary_buy_symbol") or "",
        "option_type": row.get("primary_buy_option_type") or "",
        "strike": fmt_number(row.get("primary_buy_strike")),
        "entry": fmt_money(row.get("effective_entry_price")),
        "entry_type": "actual" if row.get("actual_entry_price") is not None else (
            "planned" if row.get("primary_buy_entry_price") is not None else ""
        ),
        "target_1": fmt_money(row.get("target_1_price")),
        "stop_loss": fmt_money(row.get("stop_loss_price")),
        "latest_option_price": fmt_money(row.get("latest_option_price")),
        "max_option_price": fmt_money(row.get("max_option_price")),
        "min_option_price": fmt_money(row.get("min_option_price")),
        "pnl_pct": fmt_pct(row.get("latest_pnl_pct")),
        "pnl_points": fmt_money(row.get("latest_pnl_points")),
        "pnl_status": row.get("pnl_status") or "",
        "event_gate": row.get("event_gate_reason") or "",
        "snapshots": int(row.get("snapshot_count") or 0),
        "last_snapshot": fmt_datetime(row.get("last_snapshot_time")),
        "drift_prediction": row.get("drift_effective_prediction") or "",
        "drift_size": fmt_number(row.get("drift_position_size_pct")),
        "drift_reason": row.get("drift_overrule_reason") or "",
    }


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def parse_optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_float_list(value: str | None, default: float) -> list[float]:
    if value in (None, ""):
        return [default]
    parsed: list[float] = []
    for item in value.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            parsed.append(float(token))
        except ValueError:
            continue
    return parsed or [default]


def parse_optional_float_list(value: str | None) -> list[float | None]:
    if value in (None, ""):
        return [None]
    parsed: list[float | None] = []
    for item in value.split(","):
        token = item.strip().lower()
        if not token:
            continue
        if token in {"none", "null", "na"}:
            parsed.append(None)
            continue
        try:
            parsed.append(float(token))
        except ValueError:
            continue
    return parsed or [None]


def parse_float_values(values: list[str], default: float) -> list[float]:
    parsed: list[float] = []
    for value in values:
        for item in value.split(","):
            token = item.strip()
            if not token:
                continue
            try:
                parsed.append(float(token))
            except ValueError:
                continue
    return parsed or [default]


def parse_optional_float_values(values: list[str]) -> list[float | None]:
    parsed: list[float | None] = []
    for value in values:
        for item in value.split(","):
            token = item.strip().lower()
            if not token:
                continue
            if token in {"none", "null", "na"}:
                parsed.append(None)
                continue
            try:
                parsed.append(float(token))
            except ValueError:
                continue
    return parsed or [None]


def fmt_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def fmt_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "")


def fmt_number(value: Any) -> str:
    number = as_float(value)
    if number is None:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}"


def fmt_money(value: Any) -> str:
    number = as_float(value)
    return "" if number is None else f"{number:.2f}"


def fmt_pct(value: Any) -> str:
    number = as_float(value)
    return "" if number is None else f"{number:.2f}%"


def fmt_ret_decimal(value: Any) -> str:
    """Format a return stored as a decimal fraction (e.g. -0.00685) as a percentage string."""
    number = as_float(value)
    return "" if number is None else f"{number * 100:.2f}%"


def as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def research_controls() -> str:
    from backtest.vectorbt_research.strategy_grid import DEFAULT_VARIANTS
    from src.technical_analysis.strategy_families import get_strategy_family_registry

    registry = get_strategy_family_registry()
    family_values = sorted({registry.get_meta(v.name).family for v in DEFAULT_VARIANTS})
    type_values = sorted({registry.get_meta(v.name).strategy_type for v in DEFAULT_VARIANTS})

    target_items = "".join(
        f'<label class="md-opt"><input type="checkbox" name="target_pct" value="{value:g}"{" checked" if value == 0.05 else ""}> {int(value*100)}%</label>'
        for value in TARGET_PCT_OPTIONS
    )
    stop_loss_items = "".join(
        f'<label class="md-opt"><input type="checkbox" name="stop_loss_pct" value="{value:g}"{" checked" if value == 0.02 else ""}> {int(value*100)}%</label>'
        for value in STOP_LOSS_PCT_OPTIONS
    )
    variant_items = (
        '<label class="md-opt"><input type="checkbox" name="variants" value="__ALL__" checked> All variants</label>'
        + "".join(
            f'<label class="md-opt"><input type="checkbox" name="variants" value="{html.escape(v.name)}"> {html.escape(v.name)}</label>'
            for v in DEFAULT_VARIANTS
        )
    )
    type_options = "".join(
        f'<option value="{html.escape(strategy_type)}">{html.escape(strategy_type)}</option>'
        for strategy_type in type_values
    )
    family_options = "".join(
        f'<option value="{html.escape(family)}">{html.escape(family)}</option>'
        for family in family_values
    )
    output_links = "".join(
        f'<a class="file-link" href="{url_for("research_output_file", name=name)}" target="_blank">{label}</a>'
        for name, label in [
            ("summary", "Summary"),
            ("leaderboard", "Leaderboard CSV"),
            ("predictions", "Predictions CSV"),
            ("trades", "Trades CSV"),
            ("plans", "Plans CSV"),
            ("definitions", "Definitions CSV"),
            ("watch_promotions", "Watch Promotions CSV"),
        ]
        if (RESEARCH_OUTPUT_DIR / RESEARCH_OUTPUT_FILES[name]).exists()
    )
    output_path = html.escape(str(RESEARCH_OUTPUT_DIR))
    return f"""
<form method="post" class="research-form">
  <label>
    Start date
    <input name="start" type="date" value="{RESEARCH_DEFAULT_START.isoformat()}" required>
  </label>
  <label>
    End date
    <input name="end" type="date" value="{date.today().isoformat()}" required>
  </label>
  <label>
    Target pct(s)
    <div class="multi-drop" data-required="1">
      <button type="button" class="multi-drop-btn"><span></span><i class="md-arrow">&#9662;</i></button>
      <div class="multi-drop-panel"><div class="md-opts-scroll">{target_items}</div></div>
    </div>
  </label>
  <label>
    Stop loss pct(s)
    <div class="multi-drop" data-required="1">
      <button type="button" class="multi-drop-btn"><span></span><i class="md-arrow">&#9662;</i></button>
      <div class="multi-drop-panel"><div class="md-opts-scroll">{stop_loss_items}</div></div>
    </div>
  </label>
  <label>
    Strategy type
    <select id="leaderboard-type-filter" name="strategy_type_filter">
      <option value="">All types</option>
      {type_options}
    </select>
  </label>
  <label>
    Strategy family
    <select id="leaderboard-family-filter" name="strategy_family_filter">
      <option value="">All families</option>
      {family_options}
    </select>
  </label>
  <label>
    Variant run selection
    <div class="multi-drop" data-required="1">
      <button type="button" class="multi-drop-btn"><span></span><i class="md-arrow">&#9662;</i></button>
      <div class="multi-drop-panel">
        <input class="multi-drop-search" type="text" placeholder="&#128269; Search variants…" autocomplete="off">
        <div class="md-opts-scroll">{variant_items}</div>
      </div>
    </div>
  </label>
  <div class="run-btn-wrap">
    <button type="button" id="research-run-btn" onclick="researchRunAsync(this)">Run Research Grid</button>
  </div>
</form>
<div id="research-job-status" style="display:none;margin-top:0.75rem;padding:0.6rem 1rem;border-radius:6px;font-size:0.92rem;"></div>
<div class="output-links">
  <span>&#128190; {output_path}</span>
  {output_links}
</div>
"""

def production_controls(start: date, end: date, predicted_filter: str) -> str:
    opts = [
        ("", "All predictions"),
        ("CALL", "Call"),
        ("PUT", "Put"),
        ("NO_POSITION", "No position"),
    ]
    opts_html = "".join(
        f'<option value="{v}"{" selected" if v == predicted_filter else ""}>{label}</option>'
        for v, label in opts
    )
    return f"""
<form method="get" action="/production" class="control-grid production-filter-form" data-fragment-url="/production/table">
  <label>Start date
    <input name="start" type="date" value="{start.isoformat()}">
  </label>
  <label>End date
    <input name="end" type="date" value="{end.isoformat()}">
  </label>
  <label>Predicted
    <select name="predicted">{opts_html}</select>
  </label>
  <button type="submit">Filter</button>
  <span style="flex:1"></span>
  <button type="button" class="secondary-button" onclick="document.getElementById('global-index-dialog').showModal()">Global Indices</button>
</form>
"""


def trades_controls() -> str:
    return (
        TRADES_CONTROLS
        .replace("{{ replay_start }}", date(2026, 6, 1).isoformat())
        .replace("{{ replay_end }}", date.today().isoformat())
    )

TRADES_CONTROLS = """
<div class="trades-control-grid">
  <form method="post" class="control-grid replay-controls">
    <label>Replay start <input name="start" type="date" value="{{ replay_start }}"></label>
    <label>Replay end <input name="end" type="date" value="{{ replay_end }}"></label>
    <button name="action" value="vectorbt_replay">Replay Paper Trades</button>
  </form>
  <form method="post" class="prepare-paper-controls">
    <input name="trade_date" type="hidden" value="{{ trade_date }}">
    <div class="resolved-trade-date">
      <span>Next paper trade</span>
      <strong>{{ trade_date }}</strong>
    </div>
    <button name="action" value="prepare">Prepare Paper Signals</button>
  </form>
</div>
"""

TABLE_CARD_TEMPLATE = r"""
<section class="table-card"{% if table.section_id %} id="{{ table.section_id }}"{% endif %}>
  <h3>{{ table.title }}{% if table.rows %} <span class="subtitle">({{ table.rows }} rows)</span>{% endif %}</h3>
  {% if table.path %}<div class="path">{{ table.path }}</div>{% endif %}
  {% if error %}<div class="notice error">{{ error }}</div>{% endif %}
  {% if table.controls_html %}<div class="table-toolbar">{{ table.controls_html | safe }}</div>{% endif %}
  {% if table.html %}
    <div class="table-wrap">{{ table.html | safe }}</div>
  {% else %}
    <div class="empty">{{ table.empty_message }}</div>
  {% endif %}
</section>
"""

PAGE_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stockie26 Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #f6f8fb;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #667085;
      --line: #d9e0ea;
      --accent: #186d5d;
      --accent-soft: #e7f2ef;
      --danger: #b42318;
      --shadow: 0 12px 30px rgba(20, 30, 45, 0.07);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    header {
      padding: 20px 30px 0;
      background: #fff;
      border-bottom: 1px solid var(--line);
    }
    .topbar { display: flex; justify-content: space-between; gap: 18px; align-items: end; }
    h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    .subtitle { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
    nav { display: flex; gap: 8px; margin-top: 18px; }
    nav a {
      padding: 12px 16px;
      color: #475467;
      text-decoration: none;
      border: 1px solid transparent;
      border-bottom: 0;
      border-radius: 8px 8px 0 0;
      font-weight: 700;
      font-size: 14px;
    }
    nav a.active { background: var(--bg); color: var(--accent); border-color: var(--line); }
    main { padding: 22px 30px 34px; }
    .surface {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      gap: 22px;
      align-items: start;
      padding: 20px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }
    h2 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .hero p { margin: 6px 0 0; color: var(--muted); max-width: 760px; }
    .controls {
      padding: 16px 20px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .control-grid, .button-row {
      display: flex;
      align-items: end;
      gap: 12px;
      flex-wrap: wrap;
    }
    .trades-control-grid {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
    }
    .prepare-paper-controls {
      display: flex;
      align-items: end;
      gap: 12px;
      margin-left: auto;
      padding-left: 24px;
      border-left: 1px solid var(--line);
    }
    .resolved-trade-date {
      display: grid;
      gap: 6px;
      min-width: 145px;
      color: #344054;
      font-size: 13px;
    }
    .resolved-trade-date span { font-weight: 700; }
    .resolved-trade-date strong { padding: 10px 0; font-size: 14px; }
    label {
      display: grid;
      gap: 6px;
      color: #344054;
      font-size: 13px;
      font-weight: 700;
      min-width: 145px;
    }
    label.wide { min-width: 280px; flex: 1; }
    input, select {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px 11px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }
    select[multiple] {
      padding: 4px;
    }
    select[multiple] option {
      padding: 5px 8px;
      border-radius: 4px;
      cursor: pointer;
    }
    select[multiple] option:checked {
      background: var(--accent);
      color: #fff;
    }
    button {
      border: 0;
      border-radius: 7px;
      padding: 11px 14px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    button:hover { filter: brightness(0.95); }
    button:disabled {
      cursor: wait;
      opacity: 0.72;
      filter: none;
    }
    .btn-primary { font-size: 13px; white-space: nowrap; }
    /* ── Research form grid ──────────────────────────────── */
    .research-form {
      display: grid;
      grid-template-columns:
        116px
        116px
        160px
        160px
        150px
        minmax(240px, 1.25fr)
        minmax(220px, 1fr)
        150px;
      gap: 12px;
      align-items: end;
    }
    .research-form .run-btn-wrap {
      align-self: end;
      min-width: 0;
    }
    .research-form .run-btn-wrap button {
      width: 100%;
      min-height: 38px;
      white-space: nowrap;
    }
    .research-form label { min-width: 0; }
    .research-form input,
    .research-form select,
    .research-form .multi-drop {
      box-sizing: border-box;
      min-width: 0;
      width: 100%;
    }
    .research-form label:nth-child(6),
    .research-form label:nth-child(7) {
      min-width: 220px;
    }
    /* ── Multi-select checkbox dropdown ─────────────────── */
    .multi-drop { position: relative; }
    .multi-drop-btn {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      background: #fff;
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px 11px;
      font: inherit;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      text-align: left;
      overflow: hidden;
    }
    .multi-drop-btn:hover { background: #f8fafc; filter: none; }
    .multi-drop-btn span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
    }
    .md-arrow { flex-shrink: 0; transition: transform 0.15s; font-style: normal; }
    .multi-drop.open .md-arrow { transform: rotate(180deg); }
    .multi-drop-panel {
      display: none;
      position: absolute;
      top: calc(100% + 4px);
      left: 0;
      min-width: 100%;
      max-height: 260px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(0,0,0,.12);
      z-index: 200;
      flex-direction: column;
      overflow: hidden;
    }
    .multi-drop.open .multi-drop-panel { display: flex; }
    .multi-drop-search {
      border: none;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      padding: 8px 12px;
      font: inherit;
      font-size: 12px;
      outline: none;
      flex-shrink: 0;
    }
    .md-opts-scroll { overflow-y: auto; flex: 1; }
    .md-opt {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      font-size: 13px;
      font-weight: 400;
      color: var(--text);
      cursor: pointer;
      min-width: unset;
      border-radius: 0;
    }
    .md-opt:hover { background: #f0f4ff; }
    .md-opt.hidden { display: none; }
    .md-opt input[type="checkbox"] {
      width: 14px;
      height: 14px;
      flex-shrink: 0;
      accent-color: var(--accent);
      padding: 0;
      cursor: pointer;
    }
    .output-links {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .file-link {
      display: inline-flex;
      align-items: center;
      height: 30px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 7px;
      color: #175cd3;
      background: #fff;
      text-decoration: none;
      font-size: 12px;
      font-weight: 700;
    }
    .file-link:hover { background: #f8fafc; }
    .leaderboard-filter { min-width: 200px; max-width: 320px; }
    .secondary-button { background: #344054; }
    .summary-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
    }
    .summary .summary-head h3 { padding: 0; border: 0; background: transparent; }
    .summary-actions { display: flex; align-items: center; gap: 10px; }
    .summary-action-status { color: var(--muted); font-size: 12px; }
        dialog {
            width: min(1060px, calc(100vw - 32px));
            max-height: min(680px, calc(100vh - 40px));
            padding: 0;
            border: 1px solid var(--line);
            border-radius: 8px;
            box-shadow: 0 24px 70px rgba(20, 30, 45, 0.24);
            color: var(--text);
        }
        dialog::backdrop { background: rgba(15, 23, 42, 0.34); }
        .modal-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            padding: 14px 16px;
            border-bottom: 1px solid var(--line);
            background: #f8fafc;
        }
        .modal-head h3 { margin: 0; font-size: 16px; }
        .modal-head p { margin: 4px 0 0; }
        .icon-button {
            width: 34px;
            height: 34px;
            display: inline-grid;
            place-items: center;
            padding: 0;
            border-radius: 7px;
            background: transparent;
            color: #475467;
            border: 1px solid var(--line);
            font-size: 20px;
            line-height: 1;
            flex: 0 0 auto;
        }
        .modal-body { padding: 20px; background: #fff; overflow: hidden; }
        .modal-body .chart-wrap { position: relative; height: 420px; }
        .region-tabs {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            padding: 10px 16px 0;
            background: #f8fafc;
            border-bottom: 1px solid var(--line);
        }
        .region-tab {
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 4px 14px;
            font-size: 12px;
            font-weight: 700;
            background: #fff;
            color: #475467;
            cursor: pointer;
        }
        .region-tab:hover { background: #f0f4ff; color: var(--accent); border-color: var(--accent); }
        .region-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .notice {
      margin: 16px 20px 0;
      padding: 12px 14px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: var(--accent-soft);
      color: #164f44;
      font-size: 14px;
    }
    .notice.error { background: #fff5f5; color: var(--danger); border-color: rgba(180, 35, 24, 0.25); }
    .summary {
      margin: 16px 20px 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .summary h3, .table-card h3 {
      margin: 0;
      padding: 12px 14px;
      background: #f8fafc;
      border-bottom: 1px solid var(--line);
      font-size: 15px;
    }
    pre {
      margin: 0;
      padding: 14px;
      white-space: pre-wrap;
      font: 13px/1.55 Consolas, Monaco, "Courier New", monospace;
      max-height: 300px;
      overflow: auto;
    }
    .table-card {
      margin: 16px 20px 20px;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .table-toolbar {
      display: flex;
      align-items: end;
      gap: 14px;
      flex-wrap: wrap;
      padding: 12px 14px;
      border-bottom: 1px solid #edf0f5;
      background: #fff;
    }
    .table-toolbar .control-grid { width: 100%; align-items: end; }
    .table-toolbar .global-index-button { margin-left: auto; }
    .path {
      padding: 9px 14px;
      color: var(--muted);
      background: #fff;
      border-bottom: 1px solid #edf0f5;
      font-size: 12px;
    }
    .table-wrap {
      overflow: auto;
      max-height: 520px;
      background: #fff;
    }
    table.data-table {
      border-collapse: collapse;
      width: max-content;
      min-width: 100%;
      font-size: 13px;
    }
    .data-table th, .data-table td {
      padding: 9px 11px;
      border-bottom: 1px solid #edf0f5;
      text-align: left;
      white-space: nowrap;
    }
    .data-table th:nth-child(10), .data-table td:nth-child(10) {
      max-width: 360px;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .data-table th {
      position: sticky;
      top: 0;
      background: #f8fafc;
      color: #475569;
      z-index: 1;
      font-size: 12px;
      text-transform: uppercase;
    }
    .sortable-table th {
      cursor: pointer;
      user-select: none;
    }
    .sortable-table th:hover { background: #eef2f7; }
    .sortable-table th.sort-asc::after  { content: ' \25B2'; font-size: 10px; }
    .sortable-table th.sort-desc::after { content: ' \25BC'; font-size: 10px; }
    /* Column-header tooltips via data-col-tip attribute */
    .data-table th[data-col-tip] {
      cursor: help;
      overflow: visible;
    }
    .data-table th[data-col-tip]::after {
      content: attr(data-col-tip);
      display: none;
      position: absolute;
      top: calc(100% + 4px);
      left: 0;
      z-index: 200;
      background: #1e293b;
      color: #f1f5f9;
      padding: 9px 13px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 400;
      text-transform: none;
      letter-spacing: 0;
      max-width: 380px;
      line-height: 1.6;
      white-space: pre-line;
      box-shadow: 0 6px 24px rgba(0,0,0,0.38);
      pointer-events: none;
    }
    .data-table th[data-col-tip]:hover::after { display: block; }
    #strategy-tip {
      position: fixed;
      z-index: 9999;
      background: #1e293b;
      color: #f1f5f9;
      padding: 8px 12px;
      border-radius: 7px;
      font-size: 12px;
      max-width: 360px;
      line-height: 1.55;
      pointer-events: none;
      display: none;
      box-shadow: 0 4px 20px rgba(0,0,0,0.35);
      white-space: pre-wrap;
    }
    /* Leaderboard group row colors — 15-slot palette */
    #leaderboard-table tbody tr.lb-g0  { background: #eef3ff; }
    #leaderboard-table tbody tr.lb-g1  { background: #edfaf3; }
    #leaderboard-table tbody tr.lb-g2  { background: #fff8ed; }
    #leaderboard-table tbody tr.lb-g3  { background: #fdf0ff; }
    #leaderboard-table tbody tr.lb-g4  { background: #edfbff; }
    #leaderboard-table tbody tr.lb-g5  { background: #fffbee; }
    #leaderboard-table tbody tr.lb-g6  { background: #f3eeff; }
    #leaderboard-table tbody tr.lb-g7  { background: #edfff4; }
    #leaderboard-table tbody tr.lb-g8  { background: #fff0f6; }
    #leaderboard-table tbody tr.lb-g9  { background: #efffff; }
    #leaderboard-table tbody tr.lb-g10 { background: #fffff0; }
    #leaderboard-table tbody tr.lb-g11 { background: #fff0ee; }
    #leaderboard-table tbody tr.lb-g12 { background: #f3ffee; }
    #leaderboard-table tbody tr.lb-g13 { background: #fff4ee; }
    #leaderboard-table tbody tr.lb-g14 { background: #eef4ff; }
    /* Hover should still be visible over group color */
    #leaderboard-table tbody tr:hover td { filter: brightness(0.95); }
    .empty {
      padding: 26px 14px;
      color: var(--muted);
      background: #fff;
    }
    @media (max-width: 1500px) {
      .research-form {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }
      .research-form label:nth-child(6),
      .research-form label:nth-child(7),
      .research-form .run-btn-wrap {
        grid-column: span 2;
        min-width: 0;
      }
    }
    @media (max-width: 900px) {
      header { padding: 18px 16px 0; }
      main { padding: 16px; }
      .topbar, .hero { flex-direction: column; align-items: stretch; }
      nav { overflow-x: auto; }
      .control-grid, .button-row { align-items: stretch; flex-direction: column; }
      .trades-control-grid, .prepare-paper-controls {
        align-items: stretch;
        flex-direction: column;
      }
      .prepare-paper-controls {
        margin-left: 0;
        padding: 14px 0 0;
        border-left: 0;
        border-top: 1px solid var(--line);
      }
      .research-form {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .research-form label:nth-child(6),
      .research-form label:nth-child(7) {
        grid-column: 1 / -1;
        min-width: 0;
      }
      .research-form .run-btn-wrap { grid-column: 1 / -1; }
      .multi-drop-panel { min-width: 180px; }
      label, label.wide { min-width: 0; width: 100%; }
      button { width: 100%; }
            .icon-button { width: 34px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Stockie26 Dashboard</h1>
        <p class="subtitle">Research backtests, production prediction review, and paper trade P&amp;L.</p>
      </div>
      <div class="subtitle">Today: {{ today }}</div>
    </div>
    <nav>
      <a class="{{ 'active' if active == 'research' else '' }}" href="/research">Research</a>
      <a class="{{ 'active' if active == 'production' else '' }}" href="/production">Production Strategies</a>
      <a class="{{ 'active' if active == 'trades' else '' }}" href="/trades">Trades</a>
    </nav>
  </header>
  <main>
    <section class="surface">
      <div class="hero">
        <div>
          <h2>{{ title }}</h2>
          <p>{{ subtitle }}</p>
        </div>
        {% if active == 'production' %}
        <div style="display:flex;align-items:center;gap:10px;flex-shrink:0;">
          <a id="download-predictions-btn" class="btn-primary"
             href="/production/download"
             style="padding:11px 14px;border-radius:7px;background:#344054;color:#fff;font-weight:800;font-size:13px;text-decoration:none;white-space:nowrap;"
             title="Download all signal dates, predictions, strategies and outcomes as CSV">
            &#11123; Download CSV
          </a>
          <button id="recompute-btn" class="btn-primary" onclick="startRecompute()">Recompute Predictions</button>
        </div>
        {% endif %}
      </div>
      <!-- toast notification -->
      <div id="recompute-toast" style="display:none;position:fixed;bottom:28px;right:28px;z-index:9999;
           max-width:420px;padding:14px 18px;border-radius:10px;font-size:14px;
           box-shadow:0 6px 24px rgba(0,0,0,0.18);line-height:1.45;"></div>
      <div class="controls">{{ controls | safe }}</div>
      {% if message %}<div class="notice">{{ message }}</div>{% endif %}
      {% if error %}<div class="notice error">{{ error }}</div>{% endif %}
            {% if active == 'production' %}
                <dialog id="global-index-dialog">
                    <div class="modal-head">
                        <div>
                            <h3>Global Indices <span class="subtitle">({{ global_indices_rows }} indices)</span></h3>
                            <p class="subtitle">Daily return % — last 5 sessions. Hover a point for O/H/L/C.</p>
                        </div>
                        <button type="button" class="icon-button" aria-label="Close" onclick="document.getElementById('global-index-dialog').close()">&#215;</button>
                    </div>
                    <div class="region-tabs" id="region-tabs">
                        <button class="region-tab active" data-region="ALL">All</button>
                        <button class="region-tab" data-region="INDIA">India</button>
                        <button class="region-tab" data-region="ASIA">Asia</button>
                        <button class="region-tab" data-region="EUROPE">Europe</button>
                        <button class="region-tab" data-region="US">US</button>
                    </div>
                    <div class="modal-body">
                        <div class="chart-wrap">
                            <canvas id="global-indices-chart"></canvas>
                        </div>
                        {% if not global_indices_rows %}
                            <div class="empty">No global index rows found.</div>
                        {% endif %}
                    </div>
                </dialog>
            {% endif %}
      {% if summary %}
        <section class="summary">
          <div class="summary-head">
            <h3>{{ summary_title }}</h3>
            {% if active == 'production' %}
              <div class="summary-actions">
                <span id="analyze-misses-status" class="summary-action-status"></span>
                <button type="button" id="analyze-misses-btn" class="secondary-button" onclick="analyzeMisses(this)">Analyze Misses</button>
              </div>
            {% endif %}
          </div>
          <pre>{{ summary }}</pre>
        </section>
      {% endif %}
      {% for table in tables %}
        {{ render_table_card(table) | safe }}
      {% endfor %}
    </section>
  </main>
    <script>
        window._STRATEGY_DEFS = window._STRATEGY_DEFS || {{ strategy_defs_json | safe }};
        // ── Multi-select checkbox dropdown ──────────────────────
        (function () {
            function updateBtn(drop) {
                var span = drop.querySelector('.multi-drop-btn span');
                var allCb = drop.querySelector('input[value="__ALL__"]');
                var checked = Array.from(drop.querySelectorAll('input[type="checkbox"]:checked'));
                if (allCb && allCb.checked) {
                    span.textContent = 'All variants';
                } else if (checked.length === 0) {
                    span.textContent = 'None selected';
                } else if (checked.length === 1) {
                    span.textContent = checked[0].closest('.md-opt').textContent.trim();
                } else {
                    span.textContent = checked.length + ' selected';
                }
            }
            function filterOpts(drop, q) {
                var lq = q.toLowerCase();
                drop.querySelectorAll('.md-opt').forEach(function (opt) {
                    opt.classList.toggle('hidden', lq !== '' && !opt.textContent.trim().toLowerCase().includes(lq));
                });
            }
            function closeAll() {
                document.querySelectorAll('.multi-drop.open').forEach(function (d) { d.classList.remove('open'); });
            }
            document.addEventListener('click', closeAll);
            document.querySelectorAll('.multi-drop').forEach(function (drop) {
                var btn = drop.querySelector('.multi-drop-btn');
                var search = drop.querySelector('.multi-drop-search');
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    var wasOpen = drop.classList.contains('open');
                    closeAll();
                    if (!wasOpen) {
                        drop.classList.add('open');
                        if (search) { search.value = ''; filterOpts(drop, ''); search.focus(); }
                    }
                });
                if (search) {
                    search.addEventListener('click', function (e) { e.stopPropagation(); });
                    search.addEventListener('input', function () { filterOpts(drop, search.value); });
                }
                drop.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
                    cb.addEventListener('change', function () {
                        var allCb = drop.querySelector('input[value="__ALL__"]');
                        if (allCb) {
                            if (cb === allCb && allCb.checked) {
                                // "All" checked — uncheck everything else
                                drop.querySelectorAll('input[type="checkbox"]').forEach(function (c) {
                                    if (c !== allCb) c.checked = false;
                                });
                            } else if (cb !== allCb && cb.checked) {
                                // A specific item checked — uncheck "All"
                                allCb.checked = false;
                            }
                        }
                        updateBtn(drop);
                    });
                });
                updateBtn(drop);
            });
            // Validation: required dropdowns must have >= 1 selection (capture phase)
            document.querySelectorAll('form.research-form').forEach(function (form) {
                form.addEventListener('submit', function (e) {
                    var drops = form.querySelectorAll('.multi-drop[data-required]');
                    for (var i = 0; i < drops.length; i++) {
                        if (drops[i].querySelectorAll('input[type="checkbox"]:checked').length === 0) {
                            e.preventDefault();
                            var lbl = drops[i].closest('label');
                            alert('Please select at least one option for "' + (lbl ? lbl.childNodes[0].textContent.trim() : 'this field') + '".');
                            drops[i].classList.add('open');
                            return;
                        }
                    }
                }, true);
            });
        })();
        // ── Submit guard (disable buttons while running) ────────
        document.querySelectorAll('form').forEach(function (form) {
            form.addEventListener('submit', function (e) {
                if (e.defaultPrevented) return;
                form.querySelectorAll('button[type="submit"]').forEach(function (button) {
                    var runningText = button.dataset.runningText;
                    if (runningText) { button.textContent = runningText; }
                    button.disabled = true;
                });
            });
        });
        // ── Research Grid async run ─────────────────────────────
        function researchRunAsync(btn) {
            var form = btn.closest('form');
            // Validate required multi-drops first
            var drops = form.querySelectorAll('.multi-drop[data-required]');
            for (var i = 0; i < drops.length; i++) {
                if (drops[i].querySelectorAll('input[type="checkbox"]:checked').length === 0) {
                    var lbl = drops[i].closest('label');
                    alert('Please select at least one option for "' + (lbl ? lbl.childNodes[0].textContent.trim() : 'this field') + '".');
                    drops[i].classList.add('open');
                    return;
                }
            }
            var statusEl = document.getElementById('research-job-status');
            btn.disabled = true;
            btn.textContent = 'Starting\u2026';
            statusEl.style.display = 'block';
            statusEl.style.background = '#1e3a5f';
            statusEl.style.color = '#90caf9';
            statusEl.textContent = '\u23f3 Research grid is running in the background. You can navigate away safely.';
            var data = new FormData(form);
            fetch('/research/run', { method: 'POST', body: data })
                .then(function (r) { return r.json(); })
                .then(function (resp) {
                    if (resp.error) {
                        statusEl.style.background = '#3b1a1a';
                        statusEl.style.color = '#f87171';
                        statusEl.textContent = '\u274c ' + resp.error;
                        btn.disabled = false;
                        btn.textContent = 'Run Research Grid';
                        return;
                    }
                    var jobId = resp.job_id;
                    btn.textContent = 'Running\u2026';
                    var poll = setInterval(function () {
                        fetch('/research/status/' + jobId)
                            .then(function (r) { return r.json(); })
                            .then(function (job) {
                                if (job.state === 'done') {
                                    clearInterval(poll);
                                    statusEl.style.background = '#14381f';
                                    statusEl.style.color = '#86efac';
                                    statusEl.textContent = '\u2705 ' + job.message + ' \u2014 refreshing\u2026';
                                    btn.textContent = 'Run Research Grid';
                                    btn.disabled = false;
                                    setTimeout(function () { window.location.reload(); }, 800);
                                } else if (job.state === 'failed') {
                                    clearInterval(poll);
                                    statusEl.style.background = '#3b1a1a';
                                    statusEl.style.color = '#f87171';
                                    statusEl.textContent = '\u274c Run failed: ' + job.error;
                                    btn.disabled = false;
                                    btn.textContent = 'Run Research Grid';
                                }
                            })
                            .catch(function () { /* network hiccup — keep polling */ });
                    }, 3000);
                })
                .catch(function (err) {
                    statusEl.style.background = '#3b1a1a';
                    statusEl.style.color = '#f87171';
                    statusEl.textContent = '\u274c Network error: ' + err;
                    btn.disabled = false;
                    btn.textContent = 'Run Research Grid';
                });
        }
        // ── Production precision/recall miss analysis ──────────
        function analyzeMisses(btn) {
            var statusEl = document.getElementById('analyze-misses-status');
            btn.disabled = true;
            btn.textContent = 'Analyzing…';
            statusEl.textContent = 'Generating reports…';
            fetch('/production/analyze-misses', { method: 'POST' })
                .then(function (response) {
                    return response.json().then(function (body) {
                        if (!response.ok) throw new Error(body.error || 'Analysis failed.');
                        return body;
                    });
                })
                .then(function (body) {
                    statusEl.textContent = 'Downloading 2 CSVs…';
                    (body.downloads || []).forEach(function (url, index) {
                        setTimeout(function () {
                            var frame = document.createElement('iframe');
                            frame.hidden = true;
                            frame.src = url;
                            document.body.appendChild(frame);
                            setTimeout(function () { frame.remove(); }, 60000);
                        }, index * 400);
                    });
                    setTimeout(function () { statusEl.textContent = 'Reports downloaded.'; }, 900);
                })
                .catch(function (error) {
                    statusEl.textContent = 'Error: ' + error.message;
                })
                .finally(function () {
                    btn.disabled = false;
                    btn.textContent = 'Analyze Misses';
                });
        }
        // ── Global Indices chart ────────────────────────────────
        (function () {
            var rawData = {{ global_indices_json | safe }};
            var dialog = document.getElementById('global-index-dialog');
            var chartInst = null;
            var activeRegion = 'ALL';
            if (!dialog) return;

            var PALETTE = [
                '#2563eb','#16a34a','#dc2626','#ea580c','#9333ea',
                '#0891b2','#be185d','#ca8a04','#059669','#7c3aed',
                '#0284c7','#65a30d','#b45309','#475467'
            ];

            var REGIONS = {
                'ALL':    null,  // null = show everything
                'INDIA':  ['NIFTY50','SENSEX','INDIA_VIX'],
                'ASIA':   ['ASX200','HANG_SENG','KOSPI','NIKKEI225','NIFTY50','SENSEX','SHANGHAI','INDIA_VIX'],
                'EUROPE': ['CAC40','DAX','FTSE100'],
                'US':     ['DOW','NASDAQ','RUSSELL2000','SP500'],
            };

            // Stable colour per index name so colours don't shift when filtering
            var ALL_INDICES = Object.keys(rawData).sort();
            var INDEX_COLOUR = {};
            ALL_INDICES.forEach(function (idx, i) { INDEX_COLOUR[idx] = PALETTE[i % PALETTE.length]; });

            function buildChart() {
                var canvas = document.getElementById('global-indices-chart');
                if (!canvas || !Object.keys(rawData).length) return;
                var existing = Chart.getChart(canvas);
                if (existing) existing.destroy();
                chartInst = null;

                var allowed = REGIONS[activeRegion];  // null = all
                var visibleIndices = ALL_INDICES.filter(function (idx) {
                    return !allowed || allowed.indexOf(idx) !== -1;
                });

                // Dates where at least one visible index has a real return
                var dateSet = {};
                visibleIndices.forEach(function (idx) {
                    (rawData[idx] || []).forEach(function (p) {
                        if (p.y !== null && p.y !== undefined) { dateSet[p.date] = true; }
                    });
                });
                var labels = Object.keys(dateSet).sort();

                var datasets = visibleIndices.map(function (idx) {
                    var byDate = {};
                    (rawData[idx] || []).forEach(function (p) { byDate[p.date] = p; });
                    return {
                        label: idx,
                        data: labels.map(function (d) {
                            var p = byDate[d];
                            return p ? { x: d, y: p.y, o: p.o, h: p.h, l: p.l, c: p.c } : { x: d, y: null };
                        }),
                        borderColor: INDEX_COLOUR[idx],
                        backgroundColor: INDEX_COLOUR[idx] + '22',
                        borderWidth: 2,
                        pointRadius: 4,
                        pointHoverRadius: 7,
                        tension: 0.3,
                        spanGaps: false,
                    };
                });

                chartInst = new Chart(canvas, {
                    type: 'line',
                    data: { labels: labels, datasets: datasets },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        parsing: { xAxisKey: 'x', yAxisKey: 'y' },
                        interaction: { mode: 'point', intersect: true },
                        scales: {
                            x: {
                                type: 'category',
                                title: { display: true, text: 'Date (IST)', font: { size: 12 } },
                                ticks: { font: { size: 11 } },
                            },
                            y: {
                                title: { display: true, text: 'Return %', font: { size: 12 } },
                                ticks: {
                                    font: { size: 11 },
                                    callback: function (v) { return v + '%'; }
                                },
                            }
                        },
                        plugins: {
                            legend: {
                                position: 'bottom',
                                labels: { boxWidth: 12, padding: 14, font: { size: 11 } }
                            },
                            tooltip: {
                                filter: function (item) { return item.raw && item.raw.y != null; },
                                callbacks: {
                                    title: function (items) {
                                        return items.length ? items[0].raw.x + ' \u2014 ' + items[0].dataset.label : '';
                                    },
                                    label: function (ctx) {
                                        var p = ctx.raw;
                                        if (!p || p.y == null) return '';
                                        var ret = 'Return: ' + p.y + '%';
                                        var ohlc = 'O: ' + (p.o||'-') + '  H: ' + (p.h||'-') + '  L: ' + (p.l||'-') + '  C: ' + (p.c||'-');
                                        return [ret, ohlc];
                                    }
                                }
                            }
                        }
                    }
                });
            }

            // Region tab clicks
            document.getElementById('region-tabs').addEventListener('click', function (e) {
                var btn = e.target.closest('.region-tab');
                if (!btn) return;
                document.querySelectorAll('.region-tab').forEach(function (b) { b.classList.remove('active'); });
                btn.classList.add('active');
                activeRegion = btn.dataset.region;
                buildChart();
            });

            dialog.addEventListener('click', function (e) {
                if (e.target === dialog) dialog.close();
            });
            var _origShow = dialog.showModal.bind(dialog);
            dialog.showModal = function () {
                _origShow();
                setTimeout(buildChart, 50);
            };
        })();

        // ── Sortable tables ────────────────────────────────────────
        document.querySelectorAll('table.sortable-table').forEach(function (tbl) {
            var tbody = tbl.querySelector('tbody');
            if (!tbody) return;
            tbl.querySelectorAll('thead th').forEach(function (th, colIdx) {
                var asc = true;
                th.addEventListener('click', function () {
                    tbl.querySelectorAll('thead th').forEach(function (h) {
                        h.classList.remove('sort-asc', 'sort-desc');
                    });
                    var rows = Array.from(tbody.querySelectorAll('tr'));
                    // Detect strategy_variant column on the leaderboard table for group-aware sort
                    var isGroupCol = tbl.id === 'leaderboard-table' &&
                        th.textContent.trim().toLowerCase().replace(/[_ ]/g, '') === 'strategyvariant';
                    rows.sort(function (a, b) {
                        if (isGroupCol) {
                            // Use the lb-gX class already stamped on each row — guaranteed correct
                            var am = a.className.match(/lb-g(\d+)/);
                            var bm = b.className.match(/lb-g(\d+)/);
                            var aIdx = am ? parseInt(am[1], 10) : 9999;
                            var bIdx = bm ? parseInt(bm[1], 10) : 9999;
                            return asc ? aIdx - bIdx : bIdx - aIdx;
                        }
                        var av = (a.cells[colIdx] || {}).textContent || '';
                        var bv = (b.cells[colIdx] || {}).textContent || '';
                        var an = parseFloat(av), bn = parseFloat(bv);
                        var aNum = !isNaN(an), bNum = !isNaN(bn);
                        // NaN always sinks to the bottom regardless of sort direction
                        if (!aNum && !bNum) return av.localeCompare(bv) * (asc ? 1 : -1);
                        if (!aNum) return 1;
                        if (!bNum) return -1;
                        var cmp = an - bn;
                        return asc ? cmp : -cmp;
                    });
                    rows.forEach(function (r) { tbody.appendChild(r); });
                    th.classList.add(asc ? 'sort-asc' : 'sort-desc');
                    asc = !asc;
                });
            });
        });
        // ── Leaderboard group row colors ───────────────────────
        (function () {
            var groupColors = window._STRATEGY_GROUP_COLORS || {};
            if (!Object.keys(groupColors).length) return;
            var tbl = document.getElementById('leaderboard-table');
            if (!tbl) return;
            var headers = Array.from(tbl.querySelectorAll('thead th'));
            var colIdx = -1;
            headers.forEach(function (h, i) {
                if (h.textContent.trim().toLowerCase().replace(/[_ ]/g, '') === 'strategyvariant') colIdx = i;
            });
            if (colIdx < 0) return;
            Array.from(tbl.querySelectorAll('tbody tr')).forEach(function (row) {
                var cell = row.cells[colIdx];
                if (!cell) return;
                var name = cell.textContent.trim();
                var idx = groupColors[name];
                if (idx !== undefined) {
                    row.classList.add('lb-g' + (idx % 15));
                }
            });
        })();
        // ── Research page filters ──────────────────────────────
        (function () {
            var typeSelect = document.getElementById('leaderboard-type-filter');
            var familySelect = document.getElementById('leaderboard-family-filter');
            var startInput = document.querySelector('form.research-form input[name="start"]');
            var endInput = document.querySelector('form.research-form input[name="end"]');
            var tables = ['leaderboard-table', 'research-predictions-table', 'research-trades-table']
                .map(function (id) { return document.getElementById(id); })
                .filter(Boolean);
            if (!tables.length) return;

            function normalizedHeader(value) {
                return value.trim().toLowerCase().replace(/[_ ]/g, '');
            }
            function selectedCheckboxValues(name) {
                return Array.from(document.querySelectorAll('form.research-form input[name="' + name + '"]:checked'))
                    .map(function (input) { return input.value; })
                    .filter(function (value) { return value !== '__ALL__'; });
            }
            function normalizeNumeric(value) {
                if (value === undefined || value === null) return '';
                var text = String(value).trim();
                if (!text || text.toLowerCase() === 'nan') return '';
                if (text.toLowerCase() === 'none') return 'none';
                var num = parseFloat(text);
                if (isNaN(num)) return text.toLowerCase();
                return String(Math.round(num * 1000000) / 1000000);
            }
            function cell(row, col) {
                return col >= 0 && row.cells[col] ? row.cells[col].textContent.trim() : '';
            }
            var tableState = tables.map(function (tbl) {
                var headers = Array.from(tbl.querySelectorAll('thead th'));
                var cols = {};
                headers.forEach(function (h, i) {
                    var name = normalizedHeader(h.textContent);
                    if (name === 'strategyvariant') cols.variant = i;
                    if (name === 'strategytype') cols.type = i;
                    if (name === 'strategyfamily') cols.family = i;
                    if (name === 'signaldate') cols.signalDate = i;
                    if (name === 'targetpct') cols.targetPct = i;
                    if (name === 'stoplosspct') cols.stopLossPct = i;
                });
                return {
                    tbl: tbl,
                    rows: Array.from(tbl.querySelectorAll('tbody tr')),
                    count: tbl.closest('.table-card').querySelector('h3 .subtitle'),
                    cols: cols
                };
            });
            function applyResearchFilters() {
                var selectedType = typeSelect ? typeSelect.value : '';
                var selectedFamily = familySelect ? familySelect.value : '';
                var selectedTargets = selectedCheckboxValues('target_pct').map(normalizeNumeric);
                var selectedStops = selectedCheckboxValues('stop_loss_pct').map(normalizeNumeric);
                var selectedVariants = selectedCheckboxValues('variants');
                var allVariants = document.querySelector('form.research-form input[name="variants"][value="__ALL__"]');
                if (allVariants && allVariants.checked) selectedVariants = [];
                var startDate = startInput ? startInput.value : '';
                var endDate = endInput ? endInput.value : '';

                tableState.forEach(function (state) {
                    var visible = 0;
                    state.rows.forEach(function (row) {
                        var cols = state.cols;
                        var signalDate = cell(row, cols.signalDate).slice(0, 10);
                        var show = true;
                        if (cols.type >= 0 && selectedType) show = show && cell(row, cols.type) === selectedType;
                        if (cols.family >= 0 && selectedFamily) show = show && cell(row, cols.family) === selectedFamily;
                        if (cols.variant >= 0 && selectedVariants.length) show = show && selectedVariants.indexOf(cell(row, cols.variant)) >= 0;
                        if (cols.signalDate >= 0 && startDate) show = show && signalDate >= startDate;
                        if (cols.signalDate >= 0 && endDate) show = show && signalDate <= endDate;
                        if (cols.targetPct >= 0 && selectedTargets.length) {
                            show = show && selectedTargets.indexOf(normalizeNumeric(cell(row, cols.targetPct))) >= 0;
                        }
                        if (cols.stopLossPct >= 0 && selectedStops.length) {
                            show = show && selectedStops.indexOf(normalizeNumeric(cell(row, cols.stopLossPct))) >= 0;
                        }
                        row.style.display = show ? '' : 'none';
                        if (show) visible += 1;
                    });
                    if (state.count) {
                        state.count.textContent = '(' + visible + ' of ' + state.rows.length + ' rows)';
                    }
                });
            }
            document.querySelectorAll('form.research-form input, form.research-form select').forEach(function (input) {
                input.addEventListener('change', applyResearchFilters);
            });
            applyResearchFilters();
        })();
        // ── Strategy definition tooltips ───────────────────────
        (function () {
            var defs = window._STRATEGY_DEFS || {};
            if (!Object.keys(defs).length) return;
            var tip = document.getElementById('strategy-tip');
            if (!tip) {
                tip = document.createElement('div');
                tip.id = 'strategy-tip';
                document.body.appendChild(tip);
            }

            var strategyHeaders = {
                'strategyvariant': true,
                'strategy': true,
                'predictionstrategy': true,
                'watchedstrategy': true,
                'primarystrategy': true,
                'confirmingvariant': true,
                'priorwatchvariant': true
            };

            function cleanName(value) {
                return (value || '').replace(/\u2B50/g, '').trim();
            }

            window.attachStrategyTooltips = function (root) {
                (root || document).querySelectorAll('table.data-table').forEach(function (tbl) {
                var headers = Array.from(tbl.querySelectorAll('thead th'));
                var strategyCols = [];
                headers.forEach(function (h, i) {
                    var normalized = h.textContent.trim().toLowerCase().replace(/[_ ]/g, '');
                    if (strategyHeaders[normalized]) strategyCols.push(i);
                });
                if (!strategyCols.length) return;
                Array.from(tbl.querySelectorAll('tbody tr')).forEach(function (row) {
                    strategyCols.forEach(function (colIdx) {
                        var cell = row.cells[colIdx];
                        if (!cell) return;
                        var name = cleanName(cell.textContent);
                        var def = defs[name];
                        if (!def || cell.dataset.tooltipBound === '1') return;
                        cell.dataset.tooltipBound = '1';
                        cell.style.cursor = 'help';
                        cell.setAttribute('title', def);
                        cell.addEventListener('mouseenter', function () {
                            tip.textContent = def;
                            tip.style.display = 'block';
                        });
                        cell.addEventListener('mousemove', function (e) {
                            tip.style.left = Math.min(e.clientX + 16, window.innerWidth - 376) + 'px';
                            tip.style.top = Math.max(e.clientY - tip.offsetHeight - 8, 8) + 'px';
                        });
                        cell.addEventListener('mouseleave', function () { tip.style.display = 'none'; });
                    });
                });
            });
            };
            window.attachStrategyTooltips(document);
        })();
        // ── Production table filter ─────────────────────────────
        (function () {
            function attachProductionFilter() {
                var card = document.getElementById('production-signal-table-card');
                if (!card) return;
                var form = card.querySelector('form.production-filter-form');
                if (!form || form.dataset.ajaxBound === '1') return;
                form.dataset.ajaxBound = '1';
                form.addEventListener('submit', function (event) {
                    event.preventDefault();
                    var button = form.querySelector('button[type="submit"]');
                    var params = new URLSearchParams(new FormData(form));
                    var fragmentUrl = (form.dataset.fragmentUrl || '/production/table') + '?' + params.toString();
                    var pageUrl = (form.getAttribute('action') || '/production') + '?' + params.toString();
                    if (button) {
                        button.disabled = true;
                        button.dataset.originalText = button.textContent;
                        button.textContent = 'Filtering…';
                    }
                    fetch(fragmentUrl, { headers: { 'X-Requested-With': 'fetch' } })
                        .then(function (response) {
                            if (!response.ok) throw new Error('HTTP ' + response.status);
                            return response.text();
                        })
                        .then(function (html) {
                            var template = document.createElement('template');
                            template.innerHTML = html.trim();
                            var replacement = template.content.firstElementChild;
                            if (!replacement) throw new Error('Empty response');
                            card.replaceWith(replacement);
                            window.history.replaceState({}, '', pageUrl);
                            attachProductionFilter();
                            if (typeof window.attachStrategyTooltips === 'function') {
                                window.attachStrategyTooltips(replacement);
                            }
                        })
                        .catch(function (err) {
                            if (typeof showToast === 'function') {
                                showToast('Could not filter production table: ' + err, 'error');
                            } else {
                                window.location.href = pageUrl;
                            }
                        })
                        .finally(function () {
                            if (button) {
                                button.disabled = false;
                                button.textContent = button.dataset.originalText || 'Filter';
                            }
                        });
                });
            }
            attachProductionFilter();
        })();
        // ── Production recompute ───────────────────────────────────
        (function () {
            var toast = document.getElementById('recompute-toast');
            var btn   = document.getElementById('recompute-btn');
            if (!toast || !btn) return;
            var _poll = null;

            function showToast(msg, type) {
                // type: 'info' | 'success' | 'error'
                var styles = {
                    info:    { bg: '#1e3a5f', color: '#90caf9' },
                    success: { bg: '#14381f', color: '#86efac' },
                    error:   { bg: '#3b1a1a', color: '#f87171' },
                };
                var s = styles[type] || styles.info;
                toast.style.background = s.bg;
                toast.style.color      = s.color;
                toast.textContent      = msg;
                toast.style.display    = 'block';
                if (type !== 'info') {
                    clearTimeout(toast._hide);
                    toast._hide = setTimeout(function () { toast.style.display = 'none'; }, 7000);
                }
            }

            window.startRecompute = function () {
                var startInput = document.querySelector('form[action="/production"] input[name="start"]');
                var endInput = document.querySelector('form[action="/production"] input[name="end"]');
                var payload = {
                    start: startInput ? startInput.value : '',
                    end: endInput ? endInput.value : ''
                };
                btn.disabled    = true;
                btn.textContent = 'Running…';
                showToast('⏳ Running prediction pipeline for ' + payload.start + ' to ' + payload.end + ' — this takes a few minutes…', 'info');
                fetch('/production/recompute', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                    .then(function (r) { return r.json(); })
                    .then(function (resp) {
                        if (resp.error && resp.state !== 'running') {
                            showToast('❌ ' + resp.error, 'error');
                            btn.disabled = false; btn.textContent = 'Recompute Predictions'; return;
                        }
                        _poll = setInterval(function () {
                            fetch('/production/recompute-status')
                                .then(function (r) { return r.json(); })
                                .then(function (job) {
                                    if (job.state === 'done') {
                                        clearInterval(_poll);
                                        showToast('✅ ' + job.message + ' — refreshing…', 'success');
                                        btn.disabled = false; btn.textContent = 'Recompute Predictions';
                                        setTimeout(function () { window.location.reload(); }, 1200);
                                    } else if (job.state === 'error') {
                                        clearInterval(_poll);
                                        showToast('❌ Pipeline failed: ' + job.error, 'error');
                                        btn.disabled = false; btn.textContent = 'Recompute Predictions';
                                    }
                                })
                                .catch(function () { /* network hiccup — keep polling */ });
                        }, 4000);
                    })
                    .catch(function (err) {
                        showToast('❌ Network error: ' + err, 'error');
                        btn.disabled = false; btn.textContent = 'Recompute Predictions';
                    });
            };
        })();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
