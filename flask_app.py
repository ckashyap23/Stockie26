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

from src.common.config import get_settings, get_trade_horizon_days

load_dotenv(Path(".env"))

NIFTY_SYMBOL = "NIFTY"
MODEL_VERSION = "cascade_v1"
BASE_OUTPUT_DIR = Path("output") / "backtest" / NIFTY_SYMBOL
RESEARCH_OUTPUT_DIR = BASE_OUTPUT_DIR / "vectorbt_research"
PRODUCTION_OUTPUT_DIR = BASE_OUTPUT_DIR / "production"
TRADES_OUTPUT_DIR = BASE_OUTPUT_DIR / "vectorbt"
RESEARCH_DEFAULT_START = date(2026, 4, 1)
TARGET_PCT_OPTIONS = [0.1, 0.2, 0.3, 0.5, 0.75, 1.0]
STOP_LOSS_PCT_OPTIONS = [None, 0.05, 0.1, 0.15, 0.2, 0.3]
RESEARCH_OUTPUT_FILES = {
    "summary": "strategy_grid_summary.txt",
    "leaderboard": "strategy_grid_leaderboard.csv",
    "trades": "strategy_grid_trades.csv",
    "plans": "strategy_grid_trade_plans.csv",
    "definitions": "strategy_grid_definitions.csv",
}

app = Flask(__name__)

# ── async research job registry ─────────────────────────────────────────────
# Keyed by job_id (uuid str). States: "running" | "done" | "failed".
_RESEARCH_JOBS: dict[str, dict] = {}
_RESEARCH_JOBS_LOCK = threading.Lock()


@dataclass(frozen=True)
class PageTable:
    title: str
    path: Path | None
    html: str
    rows: int
    empty_message: str = "No rows available yet."
    controls_html: str = ""


@app.get("/")
def index():
    return redirect(url_for("research"))


@app.get("/health")
def health():
    return {"status": "ok", "app": "stockie26-flask-ui"}


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
        target_pcts = parse_float_values(request.form.getlist("target_pct"), 0.5)
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


@app.route("/research", methods=["GET", "POST"])
def research():
    # POST is kept for non-JS fallback but simply redirects — JS path uses /research/run
    message = request.args.get("message", "")
    error = request.args.get("error", "")

    leaderboard_path = RESEARCH_OUTPUT_DIR / "strategy_grid_leaderboard.csv"
    # Load strategy definitions for hover tooltips
    defs_map: dict[str, str] = {}
    defs_path = RESEARCH_OUTPUT_DIR / "strategy_grid_definitions.csv"
    if defs_path.exists():
        try:
            defs_df = pd.read_csv(defs_path)
            if "strategy_variant" in defs_df.columns:
                other_cols = [c for c in defs_df.columns if c != "strategy_variant"]
                for _, drow in defs_df.iterrows():
                    key = str(drow["strategy_variant"])
                    parts = [f"{c}: {drow[c]}" for c in other_cols
                             if pd.notna(drow[c]) and str(drow[c]).strip()]
                    defs_map[key] = "\n".join(parts)
        except Exception:
            pass

    if leaderboard_path.exists():
        try:
            lb_df = pd.read_csv(leaderboard_path).head(200)

            # Compute base group = first _-segment of strategy name
            lb_df["_group"] = lb_df["strategy_variant"].apply(lambda v: str(v).split("_")[0])
            lb_df["_wr_sort"] = pd.to_numeric(lb_df.get("win_rate_pct"), errors="coerce").fillna(-1)
            group_rank = lb_df.groupby("_group")["_wr_sort"].max().rename("_group_rank")
            lb_df = lb_df.merge(group_rank, on="_group", how="left")
            # Sort: groups ordered by their best win_rate desc, rows within group also by win_rate desc
            lb_df = lb_df.sort_values(["_group_rank", "_wr_sort"], ascending=[False, False])
            ordered_groups = list(dict.fromkeys(lb_df["_group"].tolist()))
            group_color_idx = {g: i for i, g in enumerate(ordered_groups)}

            # Apply ⭐ to eligible strategies
            eligible_names = _get_eligible_strategy_names()
            if eligible_names and "strategy_variant" in lb_df.columns:
                def _star_eligible(val: object) -> str:
                    s = str(val)
                    for en in eligible_names:
                        if s == en or s.startswith(en + "_") or s.startswith(en + " "):
                            return s + " ⭐"
                    return s
                lb_df = lb_df.copy()
                lb_df["strategy_variant"] = lb_df["strategy_variant"].map(_star_eligible)

            # Build {starred_variant_name: color_index} for JS row coloring
            group_colors_map: dict[str, int] = {
                str(row["strategy_variant"]): group_color_idx.get(str(row["_group"]), 0)
                for _, row in lb_df.iterrows()
            }
            lb_df = lb_df.drop(columns=["_group", "_wr_sort", "_group_rank"])

            # Trim to the essential display columns — drop per-side quality redundancy
            _LB_DISPLAY_COLS = [
                "strategy_variant", "target_pct",
                "plans", "trades",
                "direction_win_rate_pct",
                "wins", "losses", "win_rate_pct",
                "mean_signal_quality", "positive_quality_rate_pct",
                "total_pnl_per_lot", "avg_pnl_per_unit",
            ]
            lb_df = lb_df[[c for c in _LB_DISPLAY_COLS if c in lb_df.columns]]

            lb_html = lb_df.to_html(index=False, classes="data-table sortable-table", border=0, escape=True)
            lb_html = lb_html.replace(
                'class="dataframe data-table sortable-table"',
                'id="leaderboard-table" class="dataframe data-table sortable-table"',
                1,
            )
            scripts = ""
            if defs_map:
                scripts += f'<script>window._STRATEGY_DEFS={_json_mod.dumps(defs_map)};</script>'
            scripts += f'<script>window._STRATEGY_GROUP_COLORS={_json_mod.dumps(group_colors_map)};</script>'
            lb_html = scripts + lb_html
            leaderboard = PageTable("Strategy Leaderboard", leaderboard_path, lb_html, len(lb_df))
        except Exception as exc:
            leaderboard = PageTable("Strategy Leaderboard", leaderboard_path, "", 0,
                                    empty_message=f"Could not read CSV: {exc}")
    else:
        leaderboard = PageTable("Strategy Leaderboard", leaderboard_path, "", 0)
    trades = csv_table(
        "Research Trades",
        RESEARCH_OUTPUT_DIR / "strategy_grid_trades.csv",
        limit=200,
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
        tables=[leaderboard, trades],
        summary="",
        summary_title="",
    )


def research_output_message(output_dir: Path) -> str:
    files = [
        output_dir / "strategy_grid_leaderboard.csv",
        output_dir / "strategy_grid_definitions.csv",
        output_dir / "strategy_grid_trades.csv",
        output_dir / "strategy_grid_summary.txt",
    ]
    existing = [path for path in files if path.exists()]
    if not existing:
        return ""
    latest = max(path.stat().st_mtime for path in existing)
    refreshed_at = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M:%S")
    return f"Showing existing research outputs from {output_dir} refreshed at {refreshed_at}."


def _production_default_start() -> date:
    return date(2026, 6, 1)

def _production_default_end() -> date:
    return date.today()


def build_promoted_roster_table() -> PageTable:
    """Build a Promoted Strategies Comparison table with precision, recall, F1 per variant."""
    try:
        from src.technical_analysis.cascade.dataset import build_base, regime_frame
        from src.technical_analysis.cascade.engine import _side_precisions
        from src.technical_analysis.cascade.strategies import PROMOTED_REGIME_FAMILIES
        from src.technical_analysis.cascade.constants import (
            REGIME_PRECISION_FLOOR, MIN_FIRES, REGIME_STRESS, REGIME_CALM,
            PRODUCTION_BACKTEST_START,
        )
        from src.technical_analysis.cascade.dataset import _call_ok, _put_ok
        from src.technical_analysis.cascade.constants import CALL, PUT

        from src.technical_analysis.prediction.signal_strength import add_raw_direction, summarize_signal_quality

        resolved = add_raw_direction(build_base())
        resolved = resolved[
            (pd.to_datetime(resolved["trade_date"]) >= pd.Timestamp(PRODUCTION_BACKTEST_START))
            & resolved["next_open"].notna()
        ].reset_index(drop=True)
        rows = []
        for regime in [REGIME_STRESS, REGIME_CALM]:
            floor = REGIME_PRECISION_FLOOR[regime]
            families = PROMOTED_REGIME_FAMILIES[regime]
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
                        **call_quality,
                        "eligible": "YES" if call_elig else "-",
                    })
                # PUT side
                if npp > 0:
                    put_quality = summarize_signal_quality(
                        signals[name].where(signals[name] == PUT, "NO_POSITION"), elig_df
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
                        **put_quality,
                        "eligible": "YES" if put_elig else "-",
                    })

        df = pd.DataFrame(rows, columns=[
            "regime", "strategy", "side", "fires", "precision", "recall", "F1",
            "quality_scored_fires", "mean_signal_quality", "median_signal_quality",
            "positive_quality_rate_pct", "eligible",
        ])
        # Keep only eligible variants — those that clear the precision floor with enough fires
        df = df[df["eligible"] == "YES"].drop(columns=["eligible"])
        # Sort by F1 descending
        df = df.iloc[sorted(range(len(df)), key=lambda i: -float(df.iloc[i]["F1"]) if df.iloc[i]["F1"] not in ("n/a", "") else -1.0)]
        html_str = df.to_html(index=False, classes="data-table sortable-table", border=0, escape=True)
        unique_strategies = int(df["strategy"].nunique()) if not df.empty else 0
        return PageTable(
            title=(f"Promoted Strategies Comparison — {unique_strategies} unique strategies, "
                   f"{len(df)} eligible strategy-side rows"),
            path=None,
            html=html_str,
            rows=len(df),
        )
    except Exception as exc:
        return PageTable(
            title="Promoted Strategies Comparison",
            path=None,
            html="",
            rows=0,
            empty_message=f"Could not build roster table: {exc}",
        )


def _get_eligible_strategy_names() -> set[str]:
    """Return strategy variant names tagged [PRODUCTION] in RESEARCH_VARIANTS."""
    try:
        from backtest.vectorbt_research.strategy_grid import PROMOTED_VARIANTS
        return {v.name for v in PROMOTED_VARIANTS}
    except Exception:
        return set()


@app.get("/production")
def production():
    error = ""
    start = parse_date(request.args.get("start")) or _production_default_start()
    end = parse_date(request.args.get("end")) or _production_default_end()
    predicted_filter = request.args.get("predicted", "")

    db_rows, db_error = load_production_signal_rows(start, end)
    if db_error:
        error = db_error
    if predicted_filter:
        db_rows = [r for r in db_rows if r.get("predicted", "") == predicted_filter]

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

    db_table = PageTable(
        title="Daily Prediction & Option Selection",
        path=None,
        html=df_to_html(pd.DataFrame(db_rows)),
        rows=len(db_rows),
        empty_message="No rows found for the selected date range.",
        controls_html=production_controls(start, end, predicted_filter),
    )

    roster_table = build_promoted_roster_table()

    return render_dashboard(
        active="production",
        error=error,
        title="Stockie Prediction",
        subtitle="Production daily direction predictions joined with option selection, entry, target and P&L status.",
        controls=production_header_controls(),
        tables=[roster_table, db_table],
        summary=read_text(PRODUCTION_OUTPUT_DIR / "NIFTY_prediction_summary.txt"),
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

    paper_rows, paper_error = load_paper_trade_rows(trade_date)
    if paper_error and not error:
        error = paper_error

    paper = PageTable(
        title=f"Paper Trades For {trade_date}",
        path=None,
        html=df_to_html(pd.DataFrame(paper_rows), timezone="Asia/Kolkata"),
        rows=len(paper_rows),
        empty_message="No paper trade rows found for this date.",
    )
    executed_df, execution_error = load_live_executed_trades()
    if execution_error and not error:
        error = execution_error
    closed_df = _paper_trades_with_status(executed_df, "CLOSED")
    open_df = _paper_trades_with_status(executed_df, "OPEN")
    executed = dataframe_table(
        "Executed Paper Trades",
        executed_df,
        limit=200,
        empty_message="No executed paper trades found in the database.",
        timezone="Asia/Kolkata",
    )
    closed = dataframe_table(
        "Closed Paper Trades",
        closed_df,
        limit=200,
        empty_message="No closed paper trades found in the database.",
        timezone="Asia/Kolkata",
    )
    open_trades = dataframe_table(
        "Open Paper Trades",
        open_df,
        limit=200,
        empty_message="No open paper trades found in the database.",
        timezone="Asia/Kolkata",
    )
    vectorbt_trades = csv_table(
        "VectorBT Trade Replay",
        TRADES_OUTPUT_DIR / "vectorbt_trades.csv",
        limit=200,
        timezone="Asia/Kolkata",
    )
    summary = format_trade_summary_times(read_text(TRADES_OUTPUT_DIR / "vectorbt_summary.txt"))

    return render_dashboard(
        active="trades",
        message=message,
        error=error,
        title="Trades",
        subtitle="Prepare paper signals, review live/paper fills, and replay executed trades through VectorBT.",
        controls=trades_controls(),
        tables=[paper, executed, closed, open_trades, vectorbt_trades],
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
        title=title,
        path=path,
        html=df_to_html(df.head(limit), timezone=timezone),
        rows=len(df),
    )


def dataframe_table(
    title: str,
    df: pd.DataFrame,
    limit: int = 100,
    empty_message: str = "No rows available yet.",
    timezone: str | None = None,
) -> PageTable:
    """Render live database rows without presenting them as a generated artifact."""
    visible = df.tail(limit) if len(df) > limit else df
    return PageTable(
        title=title,
        path=None,
        html=df_to_html(visible, timezone=timezone),
        rows=len(df),
        empty_message=empty_message,
    )


def load_live_executed_trades() -> tuple[pd.DataFrame, str]:
    """Load OPEN/CLOSED paper fills directly from the execution database."""
    try:
        from backtest.vectorbt_trades.data_adapter import load_paper_executed_trades

        return load_paper_executed_trades(
            underlying=NIFTY_SYMBOL,
            model_version=MODEL_VERSION,
            mode="paper",
        ), ""
    except Exception as exc:
        return pd.DataFrame(), f"Could not load executed paper trades from Supabase: {exc}"


def _paper_trades_with_status(df: pd.DataFrame, status: str) -> pd.DataFrame:
    if df.empty or "trade_status" not in df.columns:
        return df.iloc[0:0].copy()
    return df[df["trade_status"] == status].reset_index(drop=True)


def df_to_html(df: pd.DataFrame, timezone: str | None = None) -> str:
    if df.empty:
        return ""
    display = df.copy()
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


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(errors="replace")


def format_trade_summary_times(value: str) -> str:
    """Render UTC timestamps embedded in VectorBT summary text as IST."""
    pattern = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00")

    def replace(match: re.Match[str]) -> str:
        timestamp = pd.Timestamp(match.group(0))
        return timestamp.tz_convert("Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S IST")

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
    """Resolve the execution date of the newest eligible production option selection."""
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
                      AND COALESCE(final_prediction, '') <> 'NO_POSITION'
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
                for _col in ("global_us_return_mean", "global_europe_return_mean", "global_asia_return_mean"):
                    cur.execute(
                        f'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS {_col} double precision'
                    )
                cur.execute(
                    PRODUCTION_SIGNAL_SQL,
                    {"symbol": NIFTY_SYMBOL, "model_version": MODEL_VERSION,
                     "start_date": start_date, "end_date": end_date,
                     "max_open_days": get_trade_horizon_days() * 2 + 1},
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
    # Fetch 14 extra calendar days so LAG always has a prev_close for the oldest display date
    lag_start = end_date - timedelta(days=days + 13)
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        with psycopg2.connect(settings.supabase_conn_str) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    WITH display_dates AS (
                        -- The %(days)s most recent trading dates we actually want to show
                        SELECT DISTINCT trade_date
                        FROM "GlobalIndexOhlc"
                        WHERE trade_date <= %(end_date)s
                        ORDER BY trade_date DESC
                        LIMIT %(days)s
                    ), history_rows AS (
                        -- Fetch extra history so LAG has a prev_close for the oldest display date
                        SELECT index_code, trade_date, open_price, high_price, low_price, close_price
                        FROM "GlobalIndexOhlc"
                        WHERE trade_date BETWEEN %(lag_start)s AND %(end_date)s
                    ), with_returns AS (
                        SELECT
                            index_code, trade_date, open_price, high_price, low_price, close_price,
                            LAG(close_price) OVER (PARTITION BY index_code ORDER BY trade_date) AS prev_close
                        FROM history_rows
                    )
                    SELECT index_code, trade_date, open_price, high_price, low_price, close_price,
                           CASE
                               WHEN prev_close IS NULL OR prev_close = 0 THEN NULL
                               ELSE ROUND(((close_price - prev_close) / prev_close * 100)::numeric, 2)
                           END AS return_pct
                    FROM with_returns
                    WHERE trade_date IN (SELECT trade_date FROM display_dates)
                    ORDER BY trade_date ASC, index_code
                    """,
                    {"lag_start": lag_start, "end_date": end_date, "days": days},
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
      AND trade_date BETWEEN %(start_date)s AND %(end_date)s
), option_rows AS (
    SELECT *
    FROM "NiftyOptionSelection"
    WHERE symbol = %(symbol)s
      AND model_version = %(model_version)s
      AND trade_date BETWEEN %(start_date)s AND %(end_date)s
), paper_entries AS (
    -- Actual paper fill prices when a trade was taken; NULL rows mean no fill (gate-blocked,
    -- not prepared, or chose not to trade) — production PnL falls back to planned entry.
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
        p.trade_date,
        p.next_trade_date,
        p.final_prediction,
        p.direction,
        p.global_gate_reason,
        p.global_risk_off,
        p.global_us_return_mean,
        p.global_europe_return_mean,
        p.global_asia_return_mean,
        p.actual_trade_label,
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
        CASE WHEN LOWER(COALESCE(p.regime, 'calm')) = 'stress' THEN 0.005 ELSE 0.003 END
            AS target_1_pct,
        COALESCE(pe.actual_entry_price, o.primary_buy_entry_price)
            * (1 + CASE WHEN LOWER(COALESCE(p.regime, 'calm')) = 'stress' THEN 0.005 ELSE 0.003 END)
            AS target_1_price,
        CASE WHEN LOWER(COALESCE(p.regime, 'calm')) = 'stress' THEN 0.007 ELSE 0.005 END
            AS target_2_pct,
        COALESCE(pe.actual_entry_price, o.primary_buy_entry_price)
            * (1 + CASE WHEN LOWER(COALESCE(p.regime, 'calm')) = 'stress' THEN 0.007 ELSE 0.005 END)
            AS target_2_price,
        o.stop_loss_price,
        o.no_trade_reason,
        pe.actual_entry_price
    FROM june_predictions p
    LEFT JOIN option_rows o
      ON o.symbol = p.symbol
     AND o.trade_date = p.trade_date
     AND o.model_version = p.model_version
    LEFT JOIN paper_entries pe
      ON pe.signal_trade_date = p.trade_date
     AND pe.paper_trade_date = p.next_trade_date
)
SELECT
    s.*,
    -- effective_entry: actual paper fill if trade was taken, else planned option selection price
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
        ELSE ROUND(
            (stats.pnl_exit_price - COALESCE(s.actual_entry_price, s.primary_buy_entry_price))::numeric,
            2
        )
    END AS latest_pnl_points,
    CASE
        WHEN s.primary_buy_symbol IS NULL THEN COALESCE(s.no_trade_reason, 'NO_OPTION_SELECTED')
        WHEN stats.exit_status IS NULL    THEN 'NO_SNAPSHOT_DATA'
        ELSE stats.exit_status
    END AS pnl_status
FROM selected s
LEFT JOIN LATERAL (
    WITH ohlc AS (
        -- True intraday OHLC from exchange (more accurate than snapshot polling)
        SELECT oo.high_price, oo.low_price
        FROM "OptionOhlc" oo
        JOIN "OptionInstrument" oi ON oi.id = oo.option_instrument_id
        WHERE oi.instrument_token = s.primary_buy_token
          AND oo.trade_date = s.next_trade_date
          AND oo.candle_interval = 'day'
        ORDER BY oo.loaded_at DESC
        LIMIT 1
    ),
    snaps AS (
        SELECT os.snapshot_time, os.last_price
        FROM "OptionSnapshot" os
        JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
        WHERE oi.instrument_token = s.primary_buy_token
          AND os.trade_date >= s.next_trade_date
          AND os.trade_date <= LEAST(CURRENT_DATE, s.next_trade_date + %(max_open_days)s * INTERVAL '1 day')
          AND os.last_price IS NOT NULL
    ),
    ohlc_exit AS (
        -- Day-level exit detection using true intraday high/low (catches moves missed between snapshot polls)
        SELECT CASE
            WHEN s.stop_loss_price IS NOT NULL AND o.low_price  IS NOT NULL AND o.low_price  <= s.stop_loss_price THEN 'STOP_LOSS_HIT'
            WHEN s.target_2_price  IS NOT NULL AND o.high_price IS NOT NULL AND o.high_price >= s.target_2_price  THEN 'TARGET_2_HIT'
            WHEN s.target_1_price  IS NOT NULL AND o.high_price IS NOT NULL AND o.high_price >= s.target_1_price  THEN 'TARGET_1_HIT'
        END AS exit_type
        FROM ohlc o
    ),
    snap_exit AS (
        -- Fallback: first snapshot where a target/stop was hit (used for exact exit timestamp)
        SELECT snapshot_time, last_price,
               CASE
                   WHEN s.stop_loss_price IS NOT NULL AND last_price <= s.stop_loss_price THEN 'STOP_LOSS_HIT'
                   WHEN s.target_2_price  IS NOT NULL AND last_price >= s.target_2_price  THEN 'TARGET_2_HIT'
                   WHEN s.target_1_price  IS NOT NULL AND last_price >= s.target_1_price  THEN 'TARGET_1_HIT'
               END AS exit_type
        FROM snaps
        WHERE (s.stop_loss_price IS NOT NULL AND last_price <= s.stop_loss_price)
           OR (s.target_2_price  IS NOT NULL AND last_price >= s.target_2_price)
           OR (s.target_1_price  IS NOT NULL AND last_price >= s.target_1_price)
        ORDER BY snapshot_time
        LIMIT 1
    )
    SELECT
        (SELECT MIN(snapshot_time) FROM snaps)
            AS first_snapshot_time,
        COALESCE((SELECT snapshot_time FROM snap_exit),
                 (SELECT MAX(snapshot_time) FROM snaps))
            AS last_snapshot_time,
        (SELECT last_price FROM snaps ORDER BY snapshot_time DESC LIMIT 1)
            AS latest_option_price,
        -- pnl_exit_price: if OHLC confirms a target/stop hit, use the exact target price;
        -- otherwise fall back to snapshot exit price, then latest snapshot
        COALESCE(
            CASE (SELECT exit_type FROM ohlc_exit)
                WHEN 'STOP_LOSS_HIT' THEN s.stop_loss_price
                WHEN 'TARGET_2_HIT'  THEN s.target_2_price
                WHEN 'TARGET_1_HIT'  THEN s.target_1_price
            END,
            (SELECT last_price FROM snap_exit),
            (SELECT last_price FROM snaps ORDER BY snapshot_time DESC LIMIT 1)
        )   AS pnl_exit_price,
        -- exit_status: prefer OHLC (catches intraday moves missed by snapshots), then snapshot-based
        COALESCE(
            (SELECT exit_type FROM ohlc_exit),
            CASE WHEN (SELECT COUNT(*) FROM snaps) > 0 THEN 'OPEN' ELSE 'NO_SNAPSHOT_DATA' END
        )   AS exit_status,
        (SELECT COUNT(*) FROM snaps)
            AS snapshot_count,
        -- max/min from true exchange OHLC; fall back to snapshot polling if OHLC not yet loaded
        COALESCE(
            (SELECT high_price FROM ohlc),
            (SELECT MAX(last_price) FROM snaps)
        )   AS max_option_price,
        COALESCE(
            (SELECT low_price FROM ohlc),
            (SELECT MIN(last_price) FROM snaps)
        )   AS min_option_price
) stats ON true
ORDER BY s.trade_date;
"""


def format_signal_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_date": fmt_date(row.get("trade_date")),
        "trade_date": fmt_date(row.get("next_trade_date")),
        "predicted": row.get("final_prediction") or "",
        "global_index_risk": row.get("global_gate_reason") or ("YES" if row.get("global_risk_off") else ""),
        "us_ret": fmt_ret_decimal(row.get("global_us_return_mean")),
        "europe_ret": fmt_ret_decimal(row.get("global_europe_return_mean")),
        "asia_ret": fmt_ret_decimal(row.get("global_asia_return_mean")),
        "actual_label": row.get("actual_trade_label") or "Pending",
        "max_underlying_up": fmt_pct(row.get("max_underlying_up")),
        "max_underlying_down": fmt_pct(row.get("max_underlying_down")),
        "regime": row.get("regime") or "",
        "prediction_strategy": row.get("prediction_strategy") or "",
        "strength": fmt_number(row.get("strength_score")),
        "confidence": fmt_pct(row.get("confidence_level")),
        "option_selection": row.get("selected_strategy") or row.get("no_trade_reason") or "No selection",
        "option_symbol": row.get("primary_buy_symbol") or "",
        "entry": fmt_money(row.get("effective_entry_price")),
        "entry_type": "actual" if row.get("actual_entry_price") is not None else ("planned" if row.get("primary_buy_entry_price") is not None else ""),
        "target_1": fmt_money(row.get("target_1_price")),
        "target_2": fmt_money(row.get("target_2_price")),
        "stop_loss": fmt_money(row.get("stop_loss_price")),
        "latest_option_price": fmt_money(row.get("latest_option_price")),
        "max_option_price": fmt_money(row.get("max_option_price")),
        "min_option_price": fmt_money(row.get("min_option_price")),
        "pnl_pct": fmt_pct(row.get("latest_pnl_pct")),
        "pnl_points": fmt_money(row.get("latest_pnl_points")),
        "pnl_status": row.get("pnl_status") or "",
        "snapshots": int(row.get("snapshot_count") or 0),
        "last_snapshot": fmt_datetime(row.get("last_snapshot_time")),
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

    target_items = "".join(
        f'<label class="md-opt"><input type="checkbox" name="target_pct" value="{value:g}"{" checked" if value == 0.5 else ""}> {int(value*100)}%</label>'
        for value in TARGET_PCT_OPTIONS
    )
    stop_loss_items = (
        '<label class="md-opt"><input type="checkbox" name="stop_loss_pct" value="none" checked> No stop loss</label>'
        + "".join(
            f'<label class="md-opt"><input type="checkbox" name="stop_loss_pct" value="{value:g}"> {int(value*100)}%</label>'
            for value in STOP_LOSS_PCT_OPTIONS
            if value is not None
        )
    )
    variant_items = (
        '<label class="md-opt"><input type="checkbox" name="variants" value="__ALL__" checked> All variants</label>'
        + "".join(
            f'<label class="md-opt"><input type="checkbox" name="variants" value="{html.escape(v.name)}"> {html.escape(v.name)}</label>'
            for v in DEFAULT_VARIANTS
        )
    )
    output_links = "".join(
        f'<a class="file-link" href="{url_for("research_output_file", name=name)}" target="_blank">{label}</a>'
        for name, label in [
            ("summary", "Summary"),
            ("leaderboard", "Leaderboard CSV"),
            ("trades", "Trades CSV"),
            ("plans", "Plans CSV"),
            ("definitions", "Definitions CSV"),
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
    <div class="multi-drop">
      <button type="button" class="multi-drop-btn"><span></span><i class="md-arrow">&#9662;</i></button>
      <div class="multi-drop-panel"><div class="md-opts-scroll">{stop_loss_items}</div></div>
    </div>
  </label>
  <label>
    Variant filter
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
<form method="get" action="/production" class="control-grid">
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
  <button type="button" class="secondary-button global-index-button" onclick="document.getElementById('global-index-dialog').showModal()">Global Indices</button>
</form>
"""


def production_header_controls() -> str:
    from src.technical_analysis.cascade.constants import PRODUCTION_BACKTEST_START

    return (
        '<div class="notice" style="margin:0">'
        f'<strong>Production backtest window:</strong> {PRODUCTION_BACKTEST_START} '
        'through the latest fully gradeable market date. '
        'The date and prediction filters below apply only to Daily Prediction rows.'
        '</div>'
    )


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
    /* ── Research form grid ──────────────────────────────── */
    .research-form {
      display: grid;
      grid-template-columns: 160px 160px 1fr 1fr 2fr auto;
      gap: 12px 16px;
      align-items: end;
    }
    .research-form .run-btn-wrap { align-self: end; }
    .research-form label { min-width: unset; }
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
        .secondary-button { background: #344054; }
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
      padding: 12px 14px;
      border-bottom: 1px solid #edf0f5;
      background: #fff;
    }
    .table-toolbar .control-grid {
      width: 100%;
      align-items: end;
    }
    .table-toolbar .global-index-button {
      margin-left: auto;
    }
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
      vertical-align: top;
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
      .table-toolbar .global-index-button { margin-left: 0; }
      .research-form { grid-template-columns: 1fr 1fr; }
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
      <a class="{{ 'active' if active == 'production' else '' }}" href="/production">Stockie Prediction</a>
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
      </div>
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
          <h3>{{ summary_title }}</h3>
          <pre>{{ summary }}</pre>
        </section>
      {% endif %}
      {% for table in tables %}
        <section class="table-card">
          <h3>{{ table.title }}{% if table.rows %} <span class="subtitle">({{ table.rows }} rows)</span>{% endif %}</h3>
          {% if table.path %}<div class="path">{{ table.path }}</div>{% endif %}
          {% if table.controls_html %}<div class="table-toolbar">{{ table.controls_html | safe }}</div>{% endif %}
          {% if table.html %}
            <div class="table-wrap">{{ table.html | safe }}</div>
          {% else %}
            <div class="empty">{{ table.empty_message }}</div>
          {% endif %}
        </section>
      {% endfor %}
    </section>
  </main>
    <script>
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
        // ── Strategy definition tooltips ───────────────────────
        (function () {
            var defs = window._STRATEGY_DEFS || {};
            if (!Object.keys(defs).length) return;
            var tbl = document.getElementById('leaderboard-table');
            if (!tbl) return;
            var headers = Array.from(tbl.querySelectorAll('thead th'));
            var colIdx = -1;
            headers.forEach(function (h, i) {
                if (h.textContent.trim().toLowerCase().replace(/[_ ]/g, '') === 'strategyvariant') colIdx = i;
            });
            if (colIdx < 0) return;
            var tip = document.createElement('div');
            tip.id = 'strategy-tip';
            document.body.appendChild(tip);
            Array.from(tbl.querySelectorAll('tbody tr')).forEach(function (row) {
                var cell = row.cells[colIdx];
                if (!cell) return;
                var name = cell.textContent.replace(/\u2B50/g, '').trim();
                var def = defs[name];
                if (!def) return;
                cell.style.cursor = 'help';
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
        })();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
