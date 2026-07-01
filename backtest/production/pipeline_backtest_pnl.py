"""
Backtest PnL of production pipeline signals.

Reads NiftyPrediction + NiftyOptionSelection from the DB (the options the
pipeline actually selected), loads intraday OptionSnapshot prices for the
replay date, simulates exit against target/stop/time, and outputs a trade-by-
trade PnL CSV and summary.

This is Step 3 of the production backtest:
  Step 1: pipeline_backtest_prediction.py   -> NiftyPrediction rows
  Step 2: pipeline_backtest_optionselection.py -> NiftyOptionSelection rows
  Step 3: pipeline_backtest_pnl.py (this)   -> simulated PnL per signal

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
            date_filter += " AND p.trade_date >= %s"
            params.append(start_date)
        if end_date is not None:
            date_filter += " AND p.trade_date <= %s"
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
                p.trade_date,
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
                p.direction,
                p.actual_trade_label,
                p.primary_strategy      AS prediction_strategy,
                p.strength_score,
                p.confidence_level,
                o.selected_strategy,
                o.primary_buy_symbol,
                o.primary_buy_token,
                o.primary_buy_option_type,
                o.primary_buy_entry_price,
                o.target_1_price,
                o.target_2_price,
                o.stop_loss_enabled,
                o.stop_loss_price,
                o.no_trade_reason
            FROM "NiftyPrediction" p
            JOIN "NiftyOptionSelection" o
              ON o.symbol       = p.symbol
             AND o.trade_date   = p.trade_date
             AND o.model_version = p.model_version
            LEFT JOIN LATERAL (
                SELECT MIN(tc.calendar_date) AS next_trade_date
                FROM "TradingCalendar" tc
                WHERE tc.exchange = 'NSE'
                  AND tc.calendar_date > p.trade_date
                  AND tc.is_trading_day = true
            ) calendar_next ON true
            WHERE UPPER(p.symbol) = %s
              AND p.model_version = %s
              AND p.final_prediction NOT IN ('NO_POSITION', 'NO_TRADE')
              AND o.primary_buy_token IS NOT NULL
              AND o.primary_buy_entry_price IS NOT NULL
              {date_filter}
            ORDER BY p.trade_date
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
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "trade_date", "price", "lot_size"])

    pairs = [
        (int(row.primary_buy_token), row.replay_trade_date, row.trade_id)
        for row in trade_plans.itertuples(index=False)
        if pd.notna(row.primary_buy_token) and pd.notna(row.replay_trade_date)
    ]
    if not pairs:
        return pd.DataFrame(columns=["trade_id", "snapshot_time", "trade_date", "price", "lot_size"])

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

    Exit priority: stop_loss → target_2 → target_1 → last snapshot (TIME_EXIT).
    Entry price comes from NiftyOptionSelection.primary_buy_entry_price.
    """
    if trade_plans.empty or snapshots.empty:
        return pd.DataFrame()

    plan_by_id = trade_plans.set_index("trade_id")
    rows: list[dict[str, Any]] = []

    for trade_id, group in snapshots.groupby("trade_id"):
        if trade_id not in plan_by_id.index:
            continue
        plan = plan_by_id.loc[trade_id]
        group = group.sort_values("snapshot_time")

        entry_price = _float_or_none(plan.get("primary_buy_entry_price"))
        if entry_price is None:
            continue

        target_2 = _float_or_none(plan.get("target_2_price"))
        target_1 = _float_or_none(plan.get("target_1_price"))
        stop_loss = (
            _float_or_none(plan.get("stop_loss_price"))
            if bool(plan.get("stop_loss_enabled"))
            else None
        )
        lot_size = _float_or_none(group["lot_size"].iloc[0])

        exit_price = None
        exit_time = None
        exit_reason = "TIME_EXIT"
        last_hold_date = group["trade_date"].max()

        for row in group.itertuples(index=False):
            px = float(row.price)
            ts = pd.Timestamp(row.snapshot_time)
            if stop_loss is not None and px <= stop_loss:
                exit_price, exit_time, exit_reason = px, ts, "STOP_LOSS_HIT"
                break
            if target_2 is not None and px >= target_2:
                exit_price, exit_time, exit_reason = px, ts, "TARGET_2_HIT"
                break
            if target_1 is not None and px >= target_1:
                exit_price, exit_time, exit_reason = px, ts, "TARGET_1_HIT"
                break
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
            "entry_snapshot_time": entry_snap,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "exit_reason": exit_reason,
            "pnl_per_unit": round(pnl_unit, 4),
            "pnl_per_lot": round(pnl_lot, 2) if pnl_lot is not None else None,
            "return_pct": round(ret_pct, 4) if ret_pct is not None else None,
            "target_1_price": target_1,
            "target_2_price": target_2,
            "stop_loss_price": stop_loss,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Metrics + output
# ---------------------------------------------------------------------------

def _compute_metrics(trades: pd.DataFrame) -> dict[str, Any]:
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
    no_snapshot = signals[~signals["trade_id"].isin(snap_ids)].copy()
    print(f"  {len(snap_ids)} of {len(signals)} signal(s) have snapshot data")
    if len(no_snapshot) > 0:
        print(f"  {len(no_snapshot)} signal(s) have no snapshot data (excluded from PnL):")
        for _, r in no_snapshot.iterrows():
            print(f"    {r['trade_date']} replay={r['replay_trade_date']} {r.get('primary_buy_symbol','?')}")

    print("Simulating exits...")
    trades = _simulate_exits(signals, snapshots)
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
