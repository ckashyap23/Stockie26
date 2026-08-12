from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.kite_client import KiteClient
from src.technical_analysis.cascade.global_index_features import (
    RISK_INDEXES,
    build_gap_gate_signal,
)
from src.execution.entry_gate import evaluate_promoted_call_entry

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    last_price: float
    quote_time: datetime
    best_bid_price: float | None
    best_bid_quantity: int | None
    best_ask_price: float | None
    best_ask_quantity: int | None
    raw: dict[str, Any]


def prepare_paper_signals(
    trade_date: date,
    symbol: str = "NIFTY",
    model_version: str = "cascade_v1",
) -> int:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        return db.prepare_paper_execution_signals(
            trade_date=trade_date,
            symbol=symbol,
            model_version=model_version,
            paper_platform="STOCKIE",
        )
    finally:
        db.close()


def _compute_global_gap_signal(
    conn,
    signal_trade_date: date,
    paper_trade_date: date,
) -> dict[str, Any]:
    """Compute cumulative global index gate signal for the holiday gap.

    Queries GlobalIndexOhlc for dates >= signal_trade_date and < paper_trade_date.
    Delegates to build_gap_gate_signal() which computes:
      - 12-index compound risk_off/risk_on gate (magnitude + breadth threshold)
      - 3-regional GlobalNoDisagree gate (put_agree/call_agree) — same logic
        as production cascade strategies

    Returns the build_gap_gate_signal dict plus dates_in_gap.
    Returns a neutral no-gap dict if signal_date >= paper_trade_date.
    """
    days_in_gap = (paper_trade_date - signal_trade_date).days
    no_gap: dict[str, Any] = {
        "us_mean": 0.0, "europe_mean": 0.0, "asia_mean": 0.0,
        "all_mean": 0.0, "breadth": 0.0,
        "risk_off": False, "risk_on": False,
        "put_agree": False, "call_agree": False,
        "indices": {}, "dates_covered": 0, "dates_in_gap": 0,
    }
    if signal_trade_date >= paper_trade_date:
        return no_gap

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT index_code, trade_date, close_price
            FROM "GlobalIndexOhlc"
            WHERE index_code = ANY(%s)
              AND trade_date >= %s
              AND trade_date < %s
              AND close_price IS NOT NULL
            ORDER BY index_code, trade_date
            """,
            (RISK_INDEXES, signal_trade_date, paper_trade_date),
        )
        rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=["index_code", "trade_date", "close_price"]) if rows else pd.DataFrame()
    gate = build_gap_gate_signal(df)
    return {**gate, "dates_in_gap": days_in_gap}


def enter_due_paper_trades(
    trade_date: date,
    symbol: str = "NIFTY",
    model_version: str = "cascade_v1",
    slippage_pct: float = 0.0,
    max_stale_seconds: int = 300,
) -> dict[str, int]:
    settings = get_settings()
    db = get_database_client(settings)
    kite_client = KiteClient(settings)
    kite_client.authenticate()

    db.connect()
    opened = failed = 0
    try:
        signals = db.list_paper_execution_signals(
            trade_date=trade_date,
            statuses=("PLANNED",),
            symbol=symbol,
            model_version=model_version,
        )
        for signal in signals:
            signal_id = int(signal["id"])

            # A promoted CALL that gaps down materially is not entered at the
            # open. It remains PLANNED and can enter on a later invocation once
            # live spot reclaims signal_day_close_1515 + 0.10%.
            is_promoted_call = (
                signal.get("source_final_prediction") == "NO_POSITION"
                and signal.get("promoted_prediction") == "CALL"
            )
            spot_quote = _fetch_live_underlying_quote(kite_client, symbol) if is_promoted_call else {}
            decision = evaluate_promoted_call_entry(
                final_prediction=signal.get("source_final_prediction"),
                promoted_prediction=signal.get("promoted_prediction"),
                signal_day_close_1515=_float_or_none(signal.get("signal_day_close_1515")),
                d1_open=_float_or_none(spot_quote.get("open")),
                current_spot=_float_or_none(spot_quote.get("last_price")),
            )
            db.set_paper_entry_action(
                signal_id,
                decision.entry_action,
                decision.opening_gap_pct,
                decision.reclaim_level,
            )
            if not decision.allow_entry:
                db.append_paper_trade_event(
                    signal_id,
                    decision.entry_action,
                    price=_float_or_none(spot_quote.get("last_price")),
                    message=decision.reason,
                    payload=spot_quote,
                )
                print(f"  [{decision.entry_action}] {signal.get('option_symbol')} — {decision.reason}")
                continue

            try:
                quote = fetch_live_option_quote(
                    kite_client,
                    signal["option_symbol"],
                    max_stale_seconds=max_stale_seconds,
                )
                executable_ask = quote.best_ask_price or quote.last_price
                fill_price = executable_ask * (1 + slippage_pct)
                from src.common.config import (
                    get_paper_capital_per_trade_pct,
                    get_paper_trading_capital,
                )
                from src.execution.position_sizing import size_long_option_position

                lot_size = int(signal.get("lot_size") or 1)
                base_pct = get_paper_capital_per_trade_pct()
                drift_size = signal.get("drift_position_size_pct")
                effective_pct = base_pct * float(drift_size) if drift_size and 0 < float(drift_size) <= 1 else base_pct
                lot_count, quantity = size_long_option_position(
                    entry_price=fill_price,
                    lot_size=lot_size,
                    trading_capital=get_paper_trading_capital(),
                    capital_per_trade_pct=effective_pct,
                )
                db.set_paper_trade_quantity(signal_id, quantity)
                paper_order_id = db.insert_paper_order(
                    signal_id=signal_id,
                    order_role="ENTRY",
                    side="BUY",
                    quantity=quantity,
                    requested_price=float(signal.get("planned_entry_price") or executable_ask),
                    filled_price=fill_price,
                    status="FILLED",
                    payload=json_safe(quote.raw),
                    quote_time=quote.quote_time,
                )
                db.open_paper_trade(
                    signal_id=signal_id,
                    entry_price=fill_price,
                    entry_time=quote.quote_time,
                    payload=json_safe(quote.raw),
                )
                capture_paper_order_charges(
                    db=db,
                    kite_client=kite_client,
                    signal_id=signal_id,
                    paper_order_id=paper_order_id,
                    option_symbol=signal["option_symbol"],
                    side="BUY",
                    quantity=quantity,
                    fill_price=fill_price,
                )
                opened += 1
                print(
                    f"  [OPENED] {signal['option_symbol']} lots={lot_count} "
                    f"quantity={quantity} fill={fill_price:.2f}"
                )
            except Exception as exc:
                failed += 1
                message = str(exc)
                db.insert_paper_order(
                    signal_id=signal_id,
                    order_role="ENTRY",
                    side="BUY",
                    quantity=int(signal.get("quantity") or 1),
                    requested_price=_float_or_none(signal.get("planned_entry_price")),
                    filled_price=None,
                    status="FAILED",
                    payload={},
                    error_message=message,
                )
                db.set_paper_execution_signal_status(signal_id, "FAILED", message)
                db.append_paper_trade_event(signal_id, "ENTRY_FAILED", message=message)

        skipped = len(signals) - opened - failed
    finally:
        db.close()

    return {
        "planned": len(signals),
        "opened": opened,
        "failed": failed,
        "skipped": skipped,
    }


_SPOT_QUOTE_KEYS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
}


def _fetch_live_underlying_quote(kite_client: KiteClient, symbol: str) -> dict[str, Any]:
    key = _SPOT_QUOTE_KEYS.get(symbol.upper(), f"NSE:{symbol.upper()}")
    response = kite_client.kite.quote([key])
    raw = response.get(key) or {}
    ohlc = raw.get("ohlc") or {}
    return {
        "symbol": key,
        "last_price": raw.get("last_price"),
        "open": ohlc.get("open"),
        "timestamp": str(raw.get("timestamp") or ""),
    }




def monitor_open_paper_trades(
    trade_date: date | None = None,
    symbol: str = "NIFTY",
    model_version: str = "cascade_v1",
    slippage_pct: float = 0.0,
    max_stale_seconds: int = 300,
    force_exit_time: time | None = time(15, 15),
    max_open_days: int | None = None,
) -> dict[str, int]:
    if max_open_days is None:
        from src.common.config import get_trade_horizon_days
        max_open_days = get_trade_horizon_days()
    settings = get_settings()
    db = get_database_client(settings)
    kite_client = KiteClient(settings)
    kite_client.authenticate()

    db.connect()
    updated = closed = failed = ratcheted = 0
    try:
        trades = db.list_open_paper_trades(
            trade_date=trade_date,
            symbol=symbol,
            model_version=model_version,
        )
        for trade in trades:
            signal_id = int(trade["id"])
            try:
                quote = fetch_live_option_quote(
                    kite_client,
                    trade["option_symbol"],
                    max_stale_seconds=max_stale_seconds,
                )
                entry_price = float(trade["entry_price"])
                lot_size = int(trade["lot_size"]) if trade.get("lot_size") else None
                executable_bid = quote.best_bid_price or quote.last_price
                db.update_paper_trade_mtm(
                    signal_id=signal_id,
                    current_price=executable_bid,
                    current_time=quote.quote_time,
                    entry_price=entry_price,
                    lot_size=lot_size,
                )
                updated += 1

                entry_date = trade.get("paper_trade_date")
                if isinstance(entry_date, str):
                    entry_date = date.fromisoformat(entry_date)
                trading_days_open = count_open_trading_days(
                    db.conn, entry_date, quote.quote_time.astimezone(IST).date()
                )
                exit_reason = resolve_exit_reason(
                    trade,
                    executable_bid,
                    quote.quote_time,
                    force_exit_time,
                    max_open_days,
                    trading_days_open=trading_days_open,
                )
                if exit_reason == "TARGET_HIT":
                    # One quote may clear multiple exact target steps.
                    while True:
                        levels = db.ratchet_paper_trade_targets(
                            signal_id=signal_id,
                            ratchet_price=executable_bid,
                            ratchet_time=quote.quote_time,
                            trigger="TARGET",
                            payload=json_safe(quote.raw),
                        )
                        if levels is None:
                            break
                        ratcheted += 1
                        if executable_bid < levels.target_price:
                            break
                elif exit_reason:
                    exit_price = executable_bid * (1 - slippage_pct)
                    quantity = int(trade.get("quantity") or lot_size or 1)
                    paper_order_id = db.insert_paper_order(
                        signal_id=signal_id,
                        order_role="EXIT",
                        side="SELL",
                        quantity=quantity,
                        requested_price=executable_bid,
                        filled_price=exit_price,
                        status="FILLED",
                        payload=json_safe(quote.raw),
                        quote_time=quote.quote_time,
                    )
                    db.close_paper_trade(
                        signal_id=signal_id,
                        exit_price=exit_price,
                        exit_time=quote.quote_time,
                        exit_reason=exit_reason,
                        entry_price=entry_price,
                        lot_size=lot_size,
                        payload=json_safe(quote.raw),
                    )
                    capture_paper_order_charges(
                        db=db,
                        kite_client=kite_client,
                        signal_id=signal_id,
                        paper_order_id=paper_order_id,
                        option_symbol=trade["option_symbol"],
                        side="SELL",
                        quantity=quantity,
                        fill_price=exit_price,
                    )
                    closed += 1
            except Exception as exc:
                failed += 1
                db.append_paper_trade_event(
                    signal_id,
                    "MONITOR_FAILED",
                    message=str(exc),
                )
    finally:
        db.close()

    return {"open": len(trades), "updated": updated, "ratcheted": ratcheted, "closed": closed, "failed": failed}


def fetch_live_option_quote(
    kite_client: KiteClient,
    tradingsymbol: str,
    max_stale_seconds: int = 300,
) -> LiveQuote:
    kite_symbol = f"NFO:{tradingsymbol}"
    response = kite_client.fetch_quote_bulk([kite_symbol])
    quote = response.get(kite_symbol)
    if not quote:
        raise RuntimeError(f"No Kite quote returned for {kite_symbol}")

    last_price = _float_or_none(quote.get("last_price"))
    if last_price is None or last_price <= 0:
        raise RuntimeError(f"Invalid last_price for {kite_symbol}: {quote.get('last_price')}")

    quote_time = quote.get("last_trade_time") or quote.get("timestamp") or datetime.now(IST)
    if isinstance(quote_time, str):
        quote_time = datetime.fromisoformat(quote_time)
    if quote_time.tzinfo is None:
        quote_time = quote_time.replace(tzinfo=IST)
    quote_time = quote_time.astimezone(IST)

    age = (datetime.now(IST) - quote_time).total_seconds()
    if age > max_stale_seconds:
        raise RuntimeError(
            f"Stale quote for {kite_symbol}: quote_time={quote_time.isoformat()} age={age:.0f}s"
        )

    depth = quote.get("depth") if isinstance(quote.get("depth"), dict) else {}
    bids = depth.get("buy") if isinstance(depth.get("buy"), list) else []
    asks = depth.get("sell") if isinstance(depth.get("sell"), list) else []
    best_bid = bids[0] if bids and isinstance(bids[0], dict) else {}
    best_ask = asks[0] if asks and isinstance(asks[0], dict) else {}

    return LiveQuote(
        symbol=kite_symbol,
        last_price=last_price,
        quote_time=quote_time,
        best_bid_price=_float_or_none(best_bid.get("price")),
        best_bid_quantity=_int_or_none(best_bid.get("quantity")),
        best_ask_price=_float_or_none(best_ask.get("price")),
        best_ask_quantity=_int_or_none(best_ask.get("quantity")),
        raw=quote,
    )


def capture_paper_order_charges(
    db,
    kite_client: KiteClient,
    signal_id: int,
    paper_order_id: int,
    option_symbol: str,
    side: str,
    quantity: int,
    fill_price: float,
) -> None:
    """Calculate and persist Kite charges without making paper execution fail."""
    order = {
        "order_id": f"paper-{paper_order_id}",
        "exchange": "NFO",
        "tradingsymbol": option_symbol,
        "transaction_type": side,
        "variety": "regular",
        "product": "NRML",
        "order_type": "MARKET",
        "quantity": int(quantity),
        "average_price": float(fill_price),
    }
    try:
        results = kite_client.calculate_order_charges([order])
        if not results:
            raise RuntimeError("Kite charges/orders returned no result")
        db.update_paper_order_charges(paper_order_id, charge_result=json_safe(results[0]))
    except Exception as exc:
        try:
            db.update_paper_order_charges(paper_order_id, error_message=str(exc))
        except Exception as persist_exc:
            print(f"  [CHARGES_FAILED] order={paper_order_id}: {exc}; persist failed: {persist_exc}")
        else:
            print(f"  [CHARGES_FAILED] order={paper_order_id}: {exc}")
    finally:
        try:
            db.refresh_paper_trade_costs(signal_id)
        except Exception as exc:
            print(f"  [NET_PNL_REFRESH_FAILED] signal={signal_id}: {exc}")


def resolve_exit_reason(
    trade: dict,
    price: float,
    quote_time: datetime,
    force_exit_time: time | None,
    max_open_days: int | None = None,
    trading_days_open: int | None = None,
) -> str | None:
    if max_open_days is None:
        from src.common.config import get_trade_horizon_days
        max_open_days = get_trade_horizon_days()
    target_1 = _float_or_none(trade.get("target_1_price"))
    stop_loss = _float_or_none(trade.get("stop_loss_price"))

    if stop_loss is not None and price <= stop_loss:
        return "STOP_LOSS_HIT"
    if target_1 is not None and price >= target_1:
        return "TARGET_HIT"
    if (
        max_open_days is not None
        and trading_days_open is not None
        and trading_days_open >= max_open_days
        and force_exit_time is not None
        and quote_time.astimezone(IST).time() >= force_exit_time
    ):
        return "MAX_TRADING_DAYS_EXIT"
    return None


def count_open_trading_days(conn, entry_date: date | None, as_of_date: date) -> int:
    """Count NSE sessions from entry through as-of date, with weekday fallback."""
    if entry_date is None or as_of_date < entry_date:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM "TradingCalendar"
            WHERE exchange = 'NSE'
              AND is_trading_day = true
              AND calendar_date BETWEEN %s AND %s
            """,
            (entry_date, as_of_date),
        )
        count = int(cur.fetchone()[0])
    if count > 0:
        return count
    return sum(
        1 for day in pd.date_range(entry_date, as_of_date, freq="D")
        if day.weekday() < 5
    )


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value
