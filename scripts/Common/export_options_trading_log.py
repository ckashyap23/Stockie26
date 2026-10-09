"""Export production option trades with execution, OHLC movement, and PnL."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
from typing import Any

import pandas as pd
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from backtest.production.pipeline_backtest_pnl import (
    _load_production_signals,
    _load_snapshot_prices,
    _simulate_exits,
)
from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client


DEFAULT_OUTPUT = (
    Path("output")
    / "backtest"
    / "NIFTY"
    / "production"
    / "options_trading_log.csv"
)

OHLC_SOURCE_PRIORITY = {
    "KITE_HISTORICAL_DAY_OHLC": 20,
    "KITE_QUOTE_LIVE_OHLC": 10,
}


def _load_option_ohlc(signals: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "primary_buy_token",
        "replay_trade_date",
        "option_open",
        "option_high",
        "option_low",
        "option_close",
        "option_volume",
        "option_open_interest",
        "option_ohlc_source",
        "option_ohlc_interval",
    ]
    if signals.empty:
        return pd.DataFrame(columns=columns)

    tokens = sorted(
        {
            int(value)
            for value in signals["primary_buy_token"].dropna().tolist()
        }
    )
    execution_dates = pd.to_datetime(
        signals["replay_trade_date"], errors="coerce"
    ).dropna()
    if not tokens or execution_dates.empty:
        return pd.DataFrame(columns=columns)

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    oi.instrument_token,
                    oo.trade_date,
                    oo.open_price,
                    oo.high_price,
                    oo.low_price,
                    oo.close_price,
                    oo.volume,
                    oo.open_interest,
                    oo.data_source,
                    oo.candle_interval,
                    oo.loaded_at
                FROM "OptionOhlc" oo
                JOIN "OptionInstrument" oi ON oi.id = oo.option_instrument_id
                WHERE oi.instrument_token = ANY(%s)
                  AND oo.trade_date BETWEEN %s AND %s
                ORDER BY oo.trade_date, oi.instrument_token, oo.loaded_at
                """,
                (
                    tokens,
                    execution_dates.min().date(),
                    execution_dates.max().date(),
                ),
            )
            rows = cur.fetchall()
            source_columns = [description[0] for description in cur.description]
    finally:
        db.close()

    if not rows:
        return pd.DataFrame(columns=columns)

    frame = pd.DataFrame(rows, columns=source_columns)
    frame["_priority"] = (
        frame["data_source"].map(OHLC_SOURCE_PRIORITY).fillna(0)
    )
    frame = (
        frame.sort_values(
            ["trade_date", "instrument_token", "_priority", "loaded_at"]
        )
        .drop_duplicates(["instrument_token", "trade_date"], keep="last")
        .rename(
            columns={
                "instrument_token": "primary_buy_token",
                "trade_date": "replay_trade_date",
                "open_price": "option_open",
                "high_price": "option_high",
                "low_price": "option_low",
                "close_price": "option_close",
                "volume": "option_volume",
                "open_interest": "option_open_interest",
                "data_source": "option_ohlc_source",
                "candle_interval": "option_ohlc_interval",
            }
        )
    )
    return frame.reindex(columns=columns)


def add_option_movement_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    numeric_columns = [
        "entry_price",
        "exit_price",
        "target_1_price",
        "stop_loss_price",
        "option_open",
        "option_high",
        "option_low",
        "option_close",
    ]
    for column in numeric_columns:
        out[column] = pd.to_numeric(out.get(column), errors="coerce")

    entry = out["entry_price"].replace(0, pd.NA)
    out["option_max_up_points"] = (out["option_high"] - entry).clip(lower=0)
    out["option_max_up_pct"] = out["option_max_up_points"] / entry
    out["option_max_down_points"] = (entry - out["option_low"]).clip(lower=0)
    out["option_max_down_pct"] = out["option_max_down_points"] / entry
    out["option_close_move_points"] = out["option_close"] - entry
    out["option_close_move_pct"] = out["option_close_move_points"] / entry
    out["option_day_range_points"] = out["option_high"] - out["option_low"]
    out["option_day_range_pct_of_entry"] = out["option_day_range_points"] / entry
    out["target_touched_by_day_high"] = (
        out["target_1_price"].notna()
        & out["option_high"].notna()
        & (out["option_high"] >= out["target_1_price"])
    ).astype(int)
    out["stop_touched_by_day_low"] = (
        out["stop_loss_price"].notna()
        & out["option_low"].notna()
        & (out["option_low"] <= out["stop_loss_price"])
    ).astype(int)
    out["both_exit_levels_touched"] = (
        out["target_touched_by_day_high"].eq(1)
        & out["stop_touched_by_day_low"].eq(1)
    ).astype(int)
    return out


def _snapshot_excursions(
    snapshots: pd.DataFrame,
    trades: pd.DataFrame,
) -> pd.DataFrame:
    columns = [
        "trade_id",
        "observed_post_entry_high",
        "observed_post_entry_low",
        "observed_post_entry_max_up_points",
        "observed_post_entry_max_up_pct",
        "observed_post_entry_max_down_points",
        "observed_post_entry_max_down_pct",
        "observed_post_entry_quote_count",
    ]
    if snapshots.empty or trades.empty:
        return pd.DataFrame(columns=columns)

    trade_lookup = trades.set_index("trade_id")
    rows: list[dict[str, Any]] = []
    for trade_id, group in snapshots.groupby("trade_id"):
        if trade_id not in trade_lookup.index:
            continue
        trade = trade_lookup.loc[trade_id]
        entry_price = pd.to_numeric(
            pd.Series([trade.get("entry_price")]), errors="coerce"
        ).iloc[0]
        entry_time = pd.to_datetime(
            trade.get("entry_snapshot_time"), errors="coerce"
        )
        exit_time = pd.to_datetime(trade.get("exit_time"), errors="coerce")
        if pd.isna(entry_price) or pd.isna(entry_time):
            continue

        observed = group.copy()
        observed["snapshot_time"] = pd.to_datetime(
            observed["snapshot_time"], errors="coerce"
        )
        observed["price"] = pd.to_numeric(observed["price"], errors="coerce")
        observed = observed[
            observed["snapshot_time"].ge(entry_time)
            & observed["price"].notna()
        ]
        if pd.notna(exit_time):
            observed = observed[observed["snapshot_time"].le(exit_time)]
        if observed.empty:
            continue

        high = float(observed["price"].max())
        low = float(observed["price"].min())
        rows.append(
            {
                "trade_id": trade_id,
                "observed_post_entry_high": high,
                "observed_post_entry_low": low,
                "observed_post_entry_max_up_points": max(
                    0.0, high - float(entry_price)
                ),
                "observed_post_entry_max_up_pct": max(
                    0.0, high / float(entry_price) - 1
                ),
                "observed_post_entry_max_down_points": max(
                    0.0, float(entry_price) - low
                ),
                "observed_post_entry_max_down_pct": max(
                    0.0, 1 - low / float(entry_price)
                ),
                "observed_post_entry_quote_count": len(observed),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def build_options_trading_log(
    start: date,
    end: date,
    underlying: str = "NIFTY",
    model_version: str = "cascade_v1",
) -> pd.DataFrame:
    if start > end:
        raise ValueError("start must be on or before end")

    signals = _load_production_signals(
        underlying.upper(), model_version, start, end
    )
    if signals.empty:
        return pd.DataFrame()

    snapshots = _load_snapshot_prices(signals)
    trades = _simulate_exits(signals, snapshots)

    signal_columns = [
        "trade_id",
        "trade_date",
        "replay_trade_date",
        "replay_date_source",
        "final_prediction",
        "effective_prediction",
        "direction",
        "actual_trade_label",
        "actual_trade_label_relaxed",
        "prediction_strategy",
        "strength_score",
        "confidence_level",
        "selected_strategy",
        "selection_spot_price",
        "selection_as_of_time",
        "selection_score",
        "selection_confidence",
        "primary_buy_token",
        "primary_buy_symbol",
        "primary_buy_strike",
        "primary_buy_expiry",
        "primary_buy_option_type",
        "primary_buy_entry_price",
        "primary_buy_iv",
        "primary_buy_delta",
        "entry_underlying_price",
        "target_1_pct",
        "stop_loss_pct",
        "stop_loss_enabled",
        "no_trade_reason",
    ]
    base = signals.reindex(columns=signal_columns).rename(
        columns={"trade_date": "signal_date"}
    )
    trade_columns = [
        "trade_id",
        "entry_action",
        "entry_price",
        "entry_price_source",
        "entry_snapshot_time",
        "entry_underlying_price",
        "entry_option_delta",
        "target_1_price",
        "stop_loss_price",
        "exit_price",
        "exit_time",
        "exit_reason",
        "lot_size",
        "pnl_per_unit",
        "pnl_per_lot",
        "return_pct",
    ]
    trade_details = trades.reindex(columns=trade_columns).rename(
        columns={"entry_underlying_price": "resolved_entry_underlying_price"}
    )
    out = base.merge(trade_details, on="trade_id", how="left")
    missing_trade_data = out["entry_action"].isna()
    out.loc[missing_trade_data, "entry_action"] = "SKIPPED"
    out.loc[missing_trade_data, "entry_price_source"] = "NO_OHLC_DATA"
    out.loc[missing_trade_data, "exit_reason"] = "NO_OHLC_DATA"
    out["entry_underlying_price"] = pd.to_numeric(
        out["resolved_entry_underlying_price"], errors="coerce"
    ).fillna(pd.to_numeric(out["entry_underlying_price"], errors="coerce"))
    out = out.drop(columns=["resolved_entry_underlying_price"])

    option_ohlc = _load_option_ohlc(signals)
    out["replay_trade_date"] = pd.to_datetime(
        out["replay_trade_date"], errors="coerce"
    ).dt.date
    option_ohlc["replay_trade_date"] = pd.to_datetime(
        option_ohlc["replay_trade_date"], errors="coerce"
    ).dt.date
    out = out.merge(
        option_ohlc,
        on=["primary_buy_token", "replay_trade_date"],
        how="left",
        validate="many_to_one",
    )
    out = add_option_movement_metrics(out)
    out = out.merge(
        _snapshot_excursions(snapshots, trades),
        on="trade_id",
        how="left",
        validate="one_to_one",
    )
    out["days_to_expiry_at_entry"] = (
        pd.to_datetime(out["primary_buy_expiry"], errors="coerce")
        - pd.to_datetime(out["replay_trade_date"], errors="coerce")
    ).dt.days
    out["option_ohlc_available"] = out["option_high"].notna().astype(int)
    out["trade_gradeable"] = pd.to_numeric(
        out["pnl_per_lot"], errors="coerce"
    ).notna().astype(int)
    return out.sort_values(["signal_date", "primary_buy_token"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export selected production options with entry/exit prices, "
            "daily OHLC movement, observed excursions, and PnL."
        )
    )
    parser.add_argument("--start", required=True, help="Start signal date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End signal date YYYY-MM-DD")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--model-version", default="cascade_v1")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    output_path = Path(args.output)
    frame = build_options_trading_log(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        underlying=args.underlying,
        model_version=args.model_version,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)
    print(
        {
            "output": str(output_path),
            "rows": len(frame),
            "gradeable_trades": int(frame.get("trade_gradeable", pd.Series(dtype=int)).sum()),
            "ohlc_rows": int(frame.get("option_ohlc_available", pd.Series(dtype=int)).sum()),
            "start": str(frame["signal_date"].min()) if not frame.empty else None,
            "end": str(frame["signal_date"].max()) if not frame.empty else None,
        }
    )


if __name__ == "__main__":
    main()
