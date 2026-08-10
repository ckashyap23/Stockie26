"""
Backtest PnL of production pipeline signals.

Reads NiftyPrediction + NiftyOptionSelection from the DB (the options the
pipeline actually selected), loads intraday OptionSnapshot prices for the
replay date, simulates exit against target/stop/time, and outputs a trade-by-
trade PnL CSV and summary.

This is Step 3 of the production backtest:
  Step 1: scripts/daily_NIFTY/daily_nifty_prediction.py          -> upserts NiftyPrediction
  Step 2: backtest/production/pipeline_upsert_option_selections.py -> upserts NiftyOptionSelection
  Step 3: pipeline_backtest_pnl.py (this)                        -> simulated PnL per signal

Run:
  python backtest/production/pipeline_backtest_pnl.py --start 2026-04-01
  python backtest/production/pipeline_backtest_pnl.py --start 2026-06-01 --end 2026-06-30
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, time
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client

IST_FORCE_EXIT = time(15, 15)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_production_signals(
    underlying: str,
    model_version: str,
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    """Load NiftyPrediction JOIN NiftyOptionSelection rows with replay dates."""
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        params: list[Any] = [underlying.upper(), model_version]
        date_filter = ""
        if start_date is not None:
            date_filter += " AND p.signal_date >= %s"
            params.append(start_date)
        if end_date is not None:
            date_filter += " AND p.signal_date <= %s"
            params.append(end_date)

        sql = f"""
            CREATE TABLE IF NOT EXISTS "TradingCalendar" (
                calendar_date date NOT NULL,
                exchange varchar(10) NOT NULL,
                is_trading_day boolean NOT NULL DEFAULT false,
                is_weekly_expiry boolean NOT NULL DEFAULT false,
                is_monthly_expiry boolean NOT NULL DEFAULT false,
                is_special_session boolean NOT NULL DEFAULT false,
                notes text,
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT pk_trading_calendar PRIMARY KEY (calendar_date, exchange)
            );

            SELECT
                p.symbol,
                p.signal_date AS trade_date,
                COALESCE(calendar_next.next_trade_date,
                         o.next_trade_date,
                         p.next_trade_date) AS replay_trade_date,
                CASE
                    WHEN calendar_next.next_trade_date IS NOT NULL THEN 'trading_calendar'
                    WHEN o.next_trade_date IS NOT NULL          THEN 'option_selection'
                    WHEN p.next_trade_date IS NOT NULL          THEN 'prediction'
                    ELSE 'missing'
                END AS replay_date_source,
                p.final_prediction,
                p.promoted_prediction,
                p.effective_prediction,
                p.close_1515 AS signal_day_close_1515,
                p.next_open,
                p.next_high,
                p.direction,
                p.actual_trade_label,
                p.primary_strategy      AS prediction_strategy,
                p.strength_score,
                p.confidence_level,
                p.regime,
                o.selected_strategy,
                o.primary_buy_symbol,
                o.primary_buy_token,
                o.primary_buy_option_type,
                o.primary_buy_entry_price,
                o.target_1_pct,
                o.target_1_price,
                o.stop_loss_enabled,
                o.stop_loss_pct,
                o.stop_loss_price,
                paper_fill.entry_price AS actual_entry_price,
                paper_fill.entry_time AS actual_entry_time,
                paper_fill.exit_price AS actual_exit_price,
                paper_fill.exit_time AS actual_exit_time,
                paper_fill.exit_reason AS actual_exit_reason,
                paper_fill.pnl_points AS actual_pnl_points,
                paper_fill.pnl_per_lot AS actual_pnl_per_lot,
                paper_fill.return_pct AS actual_return_pct,
                paper_fill.lot_size AS actual_lot_size,
                o.no_trade_reason
            FROM "NiftyPrediction" p
            JOIN "NiftyOptionSelection" o
              ON o.symbol        = p.symbol
             AND o.trade_date    = p.signal_date
             AND o.model_version = p.model_version
            LEFT JOIN LATERAL (
                SELECT MIN(tc.calendar_date) AS next_trade_date
                FROM "TradingCalendar" tc
                WHERE tc.exchange = 'NSE'
                  AND tc.calendar_date > p.signal_date
                  AND tc.is_trading_day = true
            ) calendar_next ON true
            LEFT JOIN LATERAL (
                SELECT ptr.entry_price, ptr.entry_time, ptr.exit_price, ptr.exit_time,
                       ptr.exit_reason, ptr.pnl_points, ptr.pnl_per_lot, ptr.return_pct,
                       pes.lot_size
                FROM "PaperExecutionSignal" pes
                JOIN "PaperTradeResult" ptr
                  ON ptr.paper_execution_signal_id = pes.id
                WHERE pes.symbol = p.symbol
                  AND pes.model_version = p.model_version
                  AND pes.signal_trade_date = p.signal_date
                  AND pes.option_token = o.primary_buy_token
                  AND ptr.entry_price IS NOT NULL
                ORDER BY ptr.entry_time DESC NULLS LAST, pes.id DESC
                LIMIT 1
            ) paper_fill ON true
            WHERE UPPER(p.symbol) = %s
              AND p.model_version = %s
              AND (
                  p.effective_prediction IN ('CALL', 'PUT')
                  OR (p.drift_effective_prediction IS NOT NULL AND p.drift_effective_prediction IN ('CALL', 'PUT'))
              )
              AND o.primary_buy_token IS NOT NULL
              AND o.primary_buy_entry_price IS NOT NULL
              {date_filter}
            ORDER BY p.signal_date
        """
        with db.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
    finally:
        db.close()

    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    for col in ("trade_date", "replay_trade_date"):
        df[col] = pd.to_datetime(df[col]).dt.date
    df["trade_id"] = df.apply(
        lambda r: f"{r['trade_date']}_{int(r['primary_buy_token'])}",
        axis=1,
    )
    return df


def _get_hold_dates(cur, start_date: date, n: int, underlying: str = "NIFTY") -> list[date]:
    """Return up to n trading dates >= start_date from UnderlyingSnapshot."""
    cur.execute(
        'SELECT trade_date FROM "UnderlyingSnapshot" WHERE underlying=%s AND trade_date >= %s ORDER BY trade_date LIMIT %s',
        (underlying.upper(), start_date, n),
    )
    return [row[0] for row in cur.fetchall()]


def _load_snapshot_prices(trade_plans: pd.DataFrame) -> pd.DataFrame:
    """Load OptionSnapshot prices for each trade over TRADE_HORIZON_DAYS trading days."""
    if trade_plans.empty:
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "snapshot_label", "trade_date", "price", "lot_size"])

    pairs = [
        (int(row.primary_buy_token), row.replay_trade_date, row.trade_id)
        for row in trade_plans.itertuples(index=False)
        if pd.notna(row.primary_buy_token) and pd.notna(row.replay_trade_date)
    ]
    if not pairs:
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "snapshot_label", "trade_date", "price", "lot_size"])

    from src.common.config import get_trade_horizon_days
    n_hold = get_trade_horizon_days()

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        frames: list[pd.DataFrame] = []
        with db.conn.cursor() as cur:
            date_windows: dict[date, list[date]] = {}
            for token, trade_dt, trade_id in pairs:
                if trade_dt not in date_windows:
                    date_windows[trade_dt] = _get_hold_dates(cur, trade_dt, n_hold)
                hold_dates = date_windows[trade_dt] or [trade_dt]
                cur.execute(
                    """
                    SELECT
                        os.trade_date,
                        os.snapshot_time,
                        os.snapshot_label,
                        os.last_price  AS price,
                        oi.lot_size
                    FROM "OptionSnapshot" os
                    JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
                    WHERE oi.instrument_token = %s
                      AND os.trade_date = ANY(%s)
                      AND os.last_price IS NOT NULL
                      AND os.last_price > 0
                    ORDER BY os.snapshot_time
                    """,
                    (token, hold_dates),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                frame = pd.DataFrame(rows, columns=cols)
                if not frame.empty:
                    frame["trade_id"] = trade_id
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


# ---------------------------------------------------------------------------
# Exit simulation
# ---------------------------------------------------------------------------

def _simulate_exits(trade_plans: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    """Simulate exit for each trade using intraday snapshot prices.

    Entry price priority:
      1. actual_entry_price from paper fill (most accurate)
      2. M5_0930 snapshot close — simulated entry at 9:30 open using 5-min data
      3. primary_buy_entry_price from NiftyOptionSelection (fallback when no 5-min data)

    Exit priority: stop_loss → target (with cascade ratcheting) → TIME_EXIT
    at 15:15 on the last hold day (UNDERLYING_LOOKBACK_DAYS).
    """
    if trade_plans.empty or snapshots.empty:
        return pd.DataFrame()

    plan_by_id = trade_plans.set_index("trade_id")
    rows: list[dict[str, Any]] = []
    from src.common.config import get_cascade_n_cap, get_sl_divider_for_regime
    from src.execution.cascade import compute_cascade_levels

    for trade_id, group in snapshots.groupby("trade_id"):
        if trade_id not in plan_by_id.index:
            continue
        plan = plan_by_id.loc[trade_id]
        group = group.sort_values("snapshot_time")

        actual_entry_price = _float_or_none(plan.get("actual_entry_price"))
        entry_price_source = "planned_selection_fallback"

        if actual_entry_price is not None:
            # Paper fill: use actual entry price and trim snapshots to post-entry
            entry_price = actual_entry_price
            entry_price_source = "actual_paper_fill"
            actual_entry_time = _timestamp_as_ist_naive(plan.get("actual_entry_time"))
            if actual_entry_time is not None:
                snapshot_times = group["snapshot_time"].map(_timestamp_as_ist_naive)
                group = group.loc[snapshot_times >= actual_entry_time].copy()
                if group.empty:
                    continue
        else:
            # No paper fill — try M5_0930 as simulated 9:30 entry
            if "snapshot_label" in group.columns:
                m5_entry = group[group["snapshot_label"] == "M5_0930"]
            else:
                m5_entry = pd.DataFrame()
            if not m5_entry.empty:
                entry_snap_row = m5_entry.iloc[0]
                entry_price = float(entry_snap_row["price"])
                entry_price_source = "m5_0930_snapshot"
                entry_snap_ts = _timestamp_as_ist_naive(entry_snap_row["snapshot_time"])
                if entry_snap_ts is not None:
                    snapshot_times = group["snapshot_time"].map(_timestamp_as_ist_naive)
                    group = group.loc[snapshot_times >= entry_snap_ts].copy()
                    if group.empty:
                        continue
            else:
                # No paper fill and no 5-min data — record as NO_OHLC_DATA, skip simulation
                rows.append({
                    "trade_id":           trade_id,
                    "trade_date":         plan.get("trade_date"),
                    "replay_trade_date":  plan.get("replay_trade_date"),
                    "replay_date_source": plan.get("replay_date_source"),
                    "direction":          plan.get("direction"),
                    "actual_trade_label": plan.get("actual_trade_label"),
                    "prediction_strategy":plan.get("prediction_strategy"),
                    "selected_strategy":  plan.get("selected_strategy"),
                    "option_symbol":      plan.get("primary_buy_symbol"),
                    "option_type":        plan.get("primary_buy_option_type"),
                    "lot_size":           None,
                    "entry_price":        _float_or_none(plan.get("primary_buy_entry_price")),
                    "entry_price_source": "NO_OHLC_DATA",
                    "entry_action":       "SKIPPED",
                    "entry_snapshot_time":None,
                    "exit_price":         None,
                    "exit_time":          None,
                    "exit_reason":        "NO_OHLC_DATA",
                    "pnl_per_unit":       None,
                    "pnl_per_lot":        None,
                    "return_pct":         None,
                    "target_1_price":     None,
                    "stop_loss_price":    None,
                    "target_pct":         None,
                    "stop_loss_pct":      None,
                    "ratchet_count":      0,
                    "last_ratchet_price": None,
                })
                continue

        if entry_price is None:
            continue

        entry_action = "ENTER"
        if (
            plan.get("final_prediction") == "NO_POSITION"
            and plan.get("promoted_prediction") == "CALL"
        ):
            signal_close = _float_or_none(plan.get("signal_day_close_1515"))
            next_open = _float_or_none(plan.get("next_open"))
            next_high = _float_or_none(plan.get("next_high"))
            if signal_close and next_open:
                gap_pct = next_open / signal_close - 1.0
                reclaim_level = signal_close * 1.001
                if gap_pct <= -0.002:
                    if next_high is None or next_high < reclaim_level:
                        # Daily OHLC cannot identify an intraday reclaim timestamp;
                        # no observed reclaim means the promoted CALL is not entered.
                        continue
                    entry_action = "ENTER_CALL_RECLAIMED_DAILY_HIGH_PROXY"

        target_pct = _float_or_none(plan.get("target_1_pct"))
        stop_loss_pct = _float_or_none(plan.get("stop_loss_pct"))
        target_1 = entry_price * (1 + target_pct) if target_pct is not None else None
        stop_loss = (
            entry_price * (1 - stop_loss_pct)
            if bool(plan.get("stop_loss_enabled")) and stop_loss_pct is not None
            else None
        )
        lot_size = _float_or_none(group["lot_size"].iloc[0])

        exit_price = None
        exit_time = None
        exit_reason = "TIME_EXIT"
        last_hold_date = group["trade_date"].max()
        ratchet_count = 0
        last_ratchet_price = None
        cascade_base = entry_price
        sl_divider = get_sl_divider_for_regime(plan.get("regime"))
        n_cap = get_cascade_n_cap()

        for row in group.itertuples(index=False):
            px = float(row.price)
            ts = pd.Timestamp(row.snapshot_time)
            if stop_loss is not None and px <= stop_loss:
                exit_price, exit_time, exit_reason = px, ts, "STOP_LOSS_HIT"
                break
            while target_1 is not None and px >= target_1:
                ratchet_count += 1
                cascade_base = target_1
                last_ratchet_price = cascade_base
                if target_pct is None or stop_loss_pct is None:
                    break
                levels = compute_cascade_levels(
                    cascade_base, ratchet_count, target_pct, stop_loss_pct,
                    sl_divider, n_cap,
                )
                target_1 = levels.target_price
                if bool(plan.get("stop_loss_enabled")):
                    stop_loss = levels.stop_loss_price
            # Force-exit at 15:15 only on the final holding day
            if row.trade_date == last_hold_date and ts.time() >= IST_FORCE_EXIT:
                exit_price, exit_time, exit_reason = px, ts, "TIME_EXIT"
                break

        if exit_price is None:
            last = group.iloc[-1]
            exit_price = float(last["price"])
            exit_time = pd.Timestamp(last["snapshot_time"])

        entry_snap = pd.Timestamp(group["snapshot_time"].iloc[0])
        pnl_unit = exit_price - entry_price
        pnl_lot = pnl_unit * lot_size if lot_size is not None else None
        ret_pct = pnl_unit / entry_price * 100 if entry_price else None

        actual_exit_price = _float_or_none(plan.get("actual_exit_price"))
        if actual_exit_price is not None:
            exit_price = actual_exit_price
            exit_time = pd.Timestamp(plan.get("actual_exit_time"))
            exit_reason = plan.get("actual_exit_reason") or "ACTUAL_EXIT"
            pnl_unit = _float_or_none(plan.get("actual_pnl_points"))
            if pnl_unit is None:
                pnl_unit = exit_price - entry_price
            pnl_lot = _float_or_none(plan.get("actual_pnl_per_lot"))
            if pnl_lot is None and lot_size is not None:
                pnl_lot = pnl_unit * lot_size
            ret_pct = _float_or_none(plan.get("actual_return_pct"))
            if ret_pct is None and entry_price:
                ret_pct = pnl_unit / entry_price * 100

        rows.append({
            "trade_id": trade_id,
            "trade_date": plan.get("trade_date"),
            "replay_trade_date": plan.get("replay_trade_date"),
            "replay_date_source": plan.get("replay_date_source"),
            "direction": plan.get("direction"),
            "actual_trade_label": plan.get("actual_trade_label"),
            "prediction_strategy": plan.get("prediction_strategy"),
            "selected_strategy": plan.get("selected_strategy"),
            "option_symbol": plan.get("primary_buy_symbol"),
            "option_type": plan.get("primary_buy_option_type"),
            "lot_size": lot_size,
            "entry_price": entry_price,
            "entry_price_source": entry_price_source,
            "entry_action": entry_action,
            "entry_snapshot_time": entry_snap,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "exit_reason": exit_reason,
            "pnl_per_unit": round(pnl_unit, 4),
            "pnl_per_lot": round(pnl_lot, 2) if pnl_lot is not None else None,
            "return_pct": round(ret_pct, 4) if ret_pct is not None else None,
            "target_1_price": target_1,
            "stop_loss_price": stop_loss,
            "target_pct": target_pct,
            "stop_loss_pct": stop_loss_pct,
            "ratchet_count": ratchet_count,
            "last_ratchet_price": last_ratchet_price,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Metrics + output
# ---------------------------------------------------------------------------

def _compute_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "total_pnl_per_lot": 0.0, "win_rate_pct": None}
    # Exclude NO_OHLC_DATA rows from all metrics
    trades = trades[trades["exit_reason"] != "NO_OHLC_DATA"].copy()
    if trades.empty:
        return {"trades": 0, "total_pnl_per_lot": 0.0, "win_rate_pct": None}
    pnl = pd.to_numeric(trades["pnl_per_lot"], errors="coerce").fillna(0)
    n = len(trades)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    exit_counts = trades["exit_reason"].value_counts().to_dict() if "exit_reason" in trades.columns else {}
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "breakeven": n - wins - losses,
        "win_rate_pct": round(wins / n * 100, 2) if n else None,
        "total_pnl_per_lot": round(float(pnl.sum()), 2),
        "avg_pnl_per_lot": round(float(pnl.mean()), 2) if n else None,
        "best_trade_pnl": round(float(pnl.max()), 2) if n else None,
        "worst_trade_pnl": round(float(pnl.min()), 2) if n else None,
        "exit_reasons": exit_counts,
    }


def _write_outputs(
    output_dir: Path,
    signals: pd.DataFrame,
    no_snapshot: pd.DataFrame,
    trades: pd.DataFrame,
    metrics: dict[str, Any],
    underlying: str,
    model_version: str,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "signals": output_dir / "production_signals.csv",
        "no_snapshot": output_dir / "production_signals_no_snapshot.csv",
        "trades": output_dir / "production_pnl_trades.csv",
        "summary": output_dir / "production_pnl_summary.txt",
    }
    signals.to_csv(paths["signals"], index=False)
    no_snapshot.to_csv(paths["no_snapshot"], index=False)
    trades.to_csv(paths["trades"], index=False)

    lines = [
        "Production pipeline PnL backtest",
        "",
        f"underlying:    {underlying}",
        f"model_version: {model_version}",
        f"date range:    {start_date or 'all'} → {end_date or 'all'}",
        f"signals loaded: {len(signals)}",
        f"signals with snapshots: {len(signals) - len(no_snapshot)}",
        f"signals without snapshots (no intraday data): {len(no_snapshot)}",
        "",
        "--- Metrics ---",
    ]
    for key, value in metrics.items():
        lines.append(f"  {key}: {value}")
    paths["summary"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_as_ist_naive(value: Any) -> pd.Timestamp | None:
    """Normalize DB timestamps to comparable timezone-naive IST wall time."""
    if value is None or pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Step 3 of the production backtest: simulate PnL from NiftyPrediction "
            "+ NiftyOptionSelection signals using intraday OptionSnapshot prices."
        )
    )
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--model-version", default="cascade_v1", help="Model version. Default: cascade_v1")
    parser.add_argument("--start", default=None, help="Start signal date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End signal date YYYY-MM-DD")
    parser.add_argument(
        "--output-dir",
        default=str(Path("output") / "backtest" / "NIFTY" / "production"),
        help="Output directory. Default: output/backtest/NIFTY/production",
    )
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start) if args.start else None
    end_date = date.fromisoformat(args.end) if args.end else None
    underlying = args.underlying.upper()

    print(f"Loading production signals: {underlying} / {args.model_version} / {start_date} to {end_date}")
    signals = _load_production_signals(underlying, args.model_version, start_date, end_date)
    print(f"  {len(signals)} signal(s) loaded")

    if signals.empty:
        print("No signals found. Check that NiftyPrediction and NiftyOptionSelection are populated.")
        return

    print("Loading intraday OptionSnapshot prices for replay dates...")
    snapshots = _load_snapshot_prices(signals)
    snap_ids = set(snapshots["trade_id"]) if not snapshots.empty else set()
    actual_only = signals[
        (~signals["trade_id"].isin(snap_ids))
        & signals["actual_entry_price"].notna()
        & signals["actual_exit_price"].notna()
    ]
    if not actual_only.empty:
        actual_frames = pd.DataFrame({
            "trade_id": actual_only["trade_id"],
            "snapshot_time": actual_only["actual_entry_time"],
            "trade_date": actual_only["replay_trade_date"],
            "price": actual_only["actual_entry_price"],
            "lot_size": actual_only["actual_lot_size"],
        })
        snapshots = pd.concat([snapshots, actual_frames], ignore_index=True)
    snap_ids = set(snapshots["trade_id"]) if not snapshots.empty else set()
    no_snapshot = signals[~signals["trade_id"].isin(snap_ids)].copy()
    print(f"  {len(snap_ids)} of {len(signals)} signal(s) have snapshot data")
    if len(no_snapshot) > 0:
        print(f"  {len(no_snapshot)} signal(s) have no snapshot data (excluded from PnL):")
        for _, r in no_snapshot.iterrows():
            print(f"    {r['trade_date']} replay={r['replay_trade_date']} {r.get('primary_buy_symbol','?')}")

    # Build NO_OHLC_DATA rows for signals with no snapshots at all
    no_ohlc_rows = []
    for _, row in no_snapshot.iterrows():
        no_ohlc_rows.append({
            "trade_id":           row.get("trade_id"),
            "trade_date":         row.get("trade_date"),
            "replay_trade_date":  row.get("replay_trade_date"),
            "replay_date_source": row.get("replay_date_source"),
            "direction":          row.get("direction"),
            "actual_trade_label": row.get("actual_trade_label"),
            "prediction_strategy":row.get("prediction_strategy"),
            "selected_strategy":  row.get("selected_strategy"),
            "option_symbol":      row.get("primary_buy_symbol"),
            "option_type":        row.get("primary_buy_option_type"),
            "lot_size":           None,
            "entry_price":        _float_or_none(row.get("primary_buy_entry_price")),
            "entry_price_source": "NO_OHLC_DATA",
            "entry_action":       "SKIPPED",
            "entry_snapshot_time":None,
            "exit_price":         None,
            "exit_time":          None,
            "exit_reason":        "NO_OHLC_DATA",
            "pnl_per_unit":       None,
            "pnl_per_lot":        None,
            "return_pct":         None,
            "target_1_price":     None,
            "stop_loss_price":    None,
            "target_pct":         None,
            "stop_loss_pct":      None,
            "ratchet_count":      0,
            "last_ratchet_price": None,
        })

    print("Simulating exits...")
    trades = _simulate_exits(signals, snapshots)
    if no_ohlc_rows:
        trades = pd.concat([trades, pd.DataFrame(no_ohlc_rows)], ignore_index=True)
        trades = trades.sort_values("trade_date").reset_index(drop=True)
    metrics = _compute_metrics(trades)

    output_dir = Path(args.output_dir)
    paths = _write_outputs(
        output_dir, signals, no_snapshot, trades, metrics,
        underlying, args.model_version, start_date, end_date,
    )

    print(f"\n--- Results ---")
    for key, value in metrics.items():
        print(f"  {key}: {value}")
    print(f"\nOutputs:")
    for name, path in paths.items():
        print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
