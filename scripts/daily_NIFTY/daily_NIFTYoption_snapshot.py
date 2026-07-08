# scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py
"""
Capture live option quote snapshots from Kite quote() into dbo.OptionSnapshot
for ALL active / non-expired option instruments of a given underlying.

This version intentionally removes selected-chain logic:
  - no ATM calculation
  - no ATM +/- strikes_each_side filter
  - no nearest max_expiries selection
  - no curated option chain slice

It:
  - fetches current underlying spot using Kite quote()
  - loads ALL option instruments from dbo.OptionInstrument for the underlying
  - filters only by:
      underlying
      expiry >= today
      instrument_type in CE/PE
      instrument_token not null
      optional CLI filters: option_type, expiry_to, strike_min, strike_max, max_instruments
  - fetches live quotes using kite.quote()
  - stores best bid/ask depth
  - stores data_source = KITE_QUOTE_LIVE
  - calculates dbo.OptionSnapshotCalc after OptionSnapshot upsert

Upsert key:
  option_instrument_id + trade_date + snapshot_label
"""

from __future__ import annotations

import argparse
import re
import sys
import time as time_module
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.db.database_client import DatabaseClient
from src.data_manager.kite_client import KiteClient
from scripts.Common.calculate_option_snapshot_calc import Settings as CalcSettings
from scripts.Common.calculate_option_snapshot_calc import calculate_snapshot_ids


load_dotenv(project_root / ".env")

IST = ZoneInfo("Asia/Kolkata")
DATA_SOURCE = "KITE_QUOTE_LIVE"
DEFAULT_SCHEDULE_TOLERANCE_SECONDS = 300
DEFAULT_QUOTE_RETRIES = 4
DEFAULT_QUOTE_RETRY_SLEEP_SECONDS = 2.0
SNAPSHOT_LABEL_MODE_SCHEDULED = "scheduled"
SNAPSHOT_LABEL_MODE_5M = "m5"

SCHEDULED_SNAPSHOTS = {
    "OPEN_0915": time(9, 15),
    "CLOSE_1515": time(15, 15),
}

SPOT_SYMBOLS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def now_ist_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def seconds_from_clock_time(value: datetime, target: time) -> int:
    target_dt = datetime.combine(value.date(), target)
    return int((value - target_dt).total_seconds())


def five_minute_snapshot_label(snapshot_time: datetime) -> str:
    """Map a capture timestamp to the stable 5-minute bucket it belongs to."""
    slot_minute = snapshot_time.minute - (snapshot_time.minute % 5)
    slot_time = snapshot_time.replace(minute=slot_minute, second=0, microsecond=0)
    for scheduled_label, scheduled_time in SCHEDULED_SNAPSHOTS.items():
        if slot_time.time() == scheduled_time:
            return scheduled_label
    return f"M5_{slot_time:%H%M}"


def resolve_scheduled_snapshot_label(
    snapshot_time: datetime,
    explicit_label: str | None = None,
    schedule_tolerance_seconds: int | None = DEFAULT_SCHEDULE_TOLERANCE_SECONDS,
    allow_outside_window: bool = False,
    snapshot_label_mode: str = SNAPSHOT_LABEL_MODE_SCHEDULED,
) -> tuple[str, str, int, bool]:
    """
    Pick the snapshot label for a scheduler run.

    In 5-minute mode, use a stable M5_HHMM label based on the current IST slot.
    If the run is inside a configured schedule window, use that scheduled label.
    If it fires outside the window, write an ad-hoc LIVE_HHMM label instead of
    failing or polluting the scheduled OPEN_0915/CLOSE_1515 rows.
    """
    if explicit_label:
        target = SCHEDULED_SNAPSHOTS.get(explicit_label)
        if target is None:
            return explicit_label, "", 0, True
        label = explicit_label
    elif snapshot_label_mode == SNAPSHOT_LABEL_MODE_5M:
        return five_minute_snapshot_label(snapshot_time), "5m", 0, True
    else:
        label, target = min(
            SCHEDULED_SNAPSHOTS.items(),
            key=lambda item: abs(seconds_from_clock_time(snapshot_time, item[1])),
        )

    target_text = target.strftime("%H:%M:%S")
    delta_seconds = seconds_from_clock_time(snapshot_time, target)
    within_tolerance = (
        schedule_tolerance_seconds is None
        or abs(delta_seconds) <= schedule_tolerance_seconds
    )

    if not within_tolerance and not allow_outside_window:
        ad_hoc_label = f"LIVE_{snapshot_time:%H%M}"
        print(
            "Scheduled snapshot fired outside the allowed window; "
            f"capturing ad-hoc snapshot_label={ad_hoc_label} instead of {label}. "
            f"target_ist={target_text}, actual_ist={snapshot_time:%H:%M:%S}, "
            f"delta_seconds={delta_seconds}, tolerance_seconds={schedule_tolerance_seconds}."
        )
        return ad_hoc_label, target_text, delta_seconds, within_tolerance

    return label, target_text, delta_seconds, within_tolerance


def safe_table_name(name: str) -> str:
    parts = name.split(".")
    if len(parts) not in (1, 2):
        raise ValueError(f"Invalid table name: {name}")

    for part in parts:
        if not _IDENTIFIER_RE.match(part):
            raise ValueError(f"Invalid table identifier: {part}")

    return ".".join(f"[{part}]" for part in parts)


def table_object_name(name: str) -> str:
    if "." in name:
        return name
    return f"dbo.{name}"


def pg_table_name(name: str) -> str:
    short_name = name.split(".")[-1]
    if not _IDENTIFIER_RE.match(short_name):
        raise ValueError(f"Invalid table identifier: {short_name}")
    return f'"{short_name}"'


def ensure_snapshot_schema(db: DatabaseClient, snapshot_table: str) -> None:
    if getattr(db, "db_kind", "") == "postgres":
        try:
            db.create_core_tables()
        except Exception as exc:
            db.conn.rollback()
            print(f"[WARN] Supabase schema check skipped after error: {exc}")
        return

    full_name = table_object_name(snapshot_table)
    table = safe_table_name(snapshot_table)

    cursor = db.conn.cursor()

    cursor.execute(
        f"""
        IF COL_LENGTH('{full_name}', 'data_source') IS NULL
        BEGIN
            ALTER TABLE {table}
            ADD data_source VARCHAR(50) NULL;
        END
        """
    )

    cursor.execute(
        f"""
        IF NOT EXISTS (
            SELECT 1
            FROM sys.indexes
            WHERE name = 'UX_OptionSnapshot_Instrument_Date_Label'
              AND object_id = OBJECT_ID('{full_name}')
        )
        BEGIN
            CREATE UNIQUE INDEX UX_OptionSnapshot_Instrument_Date_Label
            ON {table}(option_instrument_id, trade_date, snapshot_label);
        END
        """
    )

    db.conn.commit()
    cursor.close()


def to_sql_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(IST).replace(tzinfo=None)
        return value

    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            return parsed.astimezone(IST).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def to_float(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def is_retryable_quote_error(exc: Exception) -> bool:
    message = str(exc).lower()
    retryable_fragments = (
        "read timed out",
        "readtimeout",
        "timeout",
        "temporarily unavailable",
        "connection aborted",
        "connection reset",
        "max retries exceeded",
        "too many requests",
        "rate limit",
        "502",
        "503",
        "504",
    )
    return any(fragment in message for fragment in retryable_fragments)


def kite_quote_with_retries(
    kite_client: KiteClient,
    keys: list[str],
    context: str,
    max_retries: int = DEFAULT_QUOTE_RETRIES,
    retry_sleep_seconds: float = DEFAULT_QUOTE_RETRY_SLEEP_SECONDS,
) -> dict[str, Any]:
    """
    Fetch Kite quotes with retry/backoff for transient network/API failures.

    Kite occasionally raises requests ReadTimeout from quote() even when the
    market API is otherwise healthy. A single transient timeout should not fail
    the scheduled snapshot job.
    """
    last_error: Exception | None = None
    attempts_made = 0

    for attempt in range(max_retries + 1):
        attempts_made = attempt + 1
        try:
            return kite_client.kite.quote(keys)
        except Exception as exc:
            last_error = exc
            retryable = is_retryable_quote_error(exc)
            if not retryable or attempt >= max_retries:
                break

            sleep_for = retry_sleep_seconds * (attempt + 1)
            print(
                "[WARN] Kite quote failed; retrying | "
                f"context={context} | attempt={attempt + 1}/{max_retries + 1} | "
                f"sleep_seconds={sleep_for:.1f} | error={exc}"
            )
            time_module.sleep(sleep_for)

    raise RuntimeError(
        f"Kite quote failed after {attempts_made} attempts | "
        f"context={context} | keys={len(keys)} | error={last_error}"
    ) from last_error


def safe_depth_item(quote: dict[str, Any], side: str, index: int = 0) -> dict[str, Any]:
    depth = quote.get("depth") or {}
    levels = depth.get(side) or []

    if len(levels) <= index or levels[index] is None:
        return {}

    return levels[index]


def get_spot_quote(
    kite_client: KiteClient,
    underlying: str,
    spot_key: str | None = None,
    max_retries: int = DEFAULT_QUOTE_RETRIES,
    retry_sleep_seconds: float = DEFAULT_QUOTE_RETRY_SLEEP_SECONDS,
) -> float:
    key = spot_key or SPOT_SYMBOLS.get(underlying.upper(), f"NSE:{underlying.upper()}")

    quote = kite_quote_with_retries(
        kite_client=kite_client,
        keys=[key],
        context=f"spot:{key}",
        max_retries=max_retries,
        retry_sleep_seconds=retry_sleep_seconds,
    )
    q = quote.get(key)

    if not q:
        raise RuntimeError(f"Could not fetch spot quote for {key}")

    spot = to_float(q.get("last_price"))

    if spot is None or spot <= 0:
        raise RuntimeError(f"Invalid spot price for {key}: {spot}")

    return spot


def load_all_live_option_instruments(
    db: DatabaseClient,
    underlying: str,
    as_of: date,
    instrument_table: str = "dbo.OptionInstrument",
    option_type: str | None = None,
    expiry_to: date | None = None,
    strike_min: float | None = None,
    strike_max: float | None = None,
    max_instruments: int | None = None,
) -> list[dict[str, Any]]:
    """
    Load all option instruments for live quote capture.

    Mandatory filters:
      - underlying = ?
      - expiry >= today
      - instrument_type IN CE/PE
      - instrument_token IS NOT NULL

    Optional filters are only safety controls.
    They are not selected-chain logic.
    """
    is_postgres = getattr(db, "db_kind", "") == "postgres"
    table = pg_table_name(instrument_table) if is_postgres else safe_table_name(instrument_table)
    placeholder = "%s" if is_postgres else "?"
    expiry_expr = "expiry" if is_postgres else "CAST(expiry AS date)"

    sql = f"""
        SELECT
            id,
            instrument_token,
            exchange,
            underlying,
            tradingsymbol,
            strike,
            {expiry_expr} AS expiry,
            instrument_type
        FROM {table}
        WHERE underlying = {placeholder}
          AND {expiry_expr} >= {placeholder}
          AND instrument_token IS NOT NULL
          AND instrument_type IN ('CE', 'PE')
    """

    params: list[Any] = [underlying, as_of]

    if option_type:
        sql += f" AND instrument_type = {placeholder}"
        params.append(option_type)

    if expiry_to:
        sql += f" AND {expiry_expr} <= {placeholder}"
        params.append(expiry_to)

    if strike_min is not None:
        sql += f" AND strike >= {placeholder}"
        params.append(strike_min)

    if strike_max is not None:
        sql += f" AND strike <= {placeholder}"
        params.append(strike_max)

    sql += f" ORDER BY {expiry_expr}, strike, instrument_type"

    cursor = db.conn.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()

    instruments: list[dict[str, Any]] = []

    for row in rows:
        if is_postgres:
            row_id, token, exchange, row_underlying, symbol, strike, expiry, row_type = row
        else:
            row_id = row.id
            token = row.instrument_token
            exchange = row.exchange
            row_underlying = row.underlying
            symbol = row.tradingsymbol
            strike = row.strike
            expiry = row.expiry
            row_type = row.instrument_type
        instruments.append({
            "id": int(row_id),
            "instrument_token": int(token) if token is not None else None,
            "exchange": exchange or "NFO",
            "underlying": row_underlying,
            "tradingsymbol": symbol,
            "strike": float(strike),
            "expiry": _to_date(expiry),
            "instrument_type": row_type,
        })

        if max_instruments is not None and len(instruments) >= max_instruments:
            break

    return instruments


def fetch_option_quotes(
    kite_client: KiteClient,
    instruments: list[dict[str, Any]],
    quote_batch_size: int = 200,
    sleep_seconds: float = 0.20,
    max_retries: int = DEFAULT_QUOTE_RETRIES,
    retry_sleep_seconds: float = DEFAULT_QUOTE_RETRY_SLEEP_SECONDS,
) -> dict[str, dict[str, Any]]:
    quote_keys = [
        f"{inst['exchange']}:{inst['tradingsymbol']}"
        for inst in instruments
        if inst.get("exchange") and inst.get("tradingsymbol")
    ]

    quotes: dict[str, dict[str, Any]] = {}

    for batch_number, keys in enumerate(chunked(quote_keys, quote_batch_size), start=1):
        quotes.update(
            kite_quote_with_retries(
                kite_client=kite_client,
                keys=keys,
                context=f"option_batch:{batch_number}",
                max_retries=max_retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
        )

        if sleep_seconds > 0:
            time_module.sleep(sleep_seconds)

    return quotes


def build_snapshot_row(
    instrument: dict[str, Any],
    kite_quote: dict[str, Any],
    underlying_price: float,
    snapshot_time: datetime,
    snapshot_label: str,
) -> dict[str, Any]:
    best_bid = safe_depth_item(kite_quote, "buy", 0)
    best_ask = safe_depth_item(kite_quote, "sell", 0)

    return {
        "option_instrument_id": instrument["id"],
        "snapshot_time": snapshot_time,
        "underlying_price": underlying_price,

        # Live Kite quote fields
        "last_price": to_float(kite_quote.get("last_price")),
        "bid_price": to_float(best_bid.get("price")),
        "bid_qty": to_int(best_bid.get("quantity")),
        "ask_price": to_float(best_ask.get("price")),
        "ask_qty": to_int(best_ask.get("quantity")),
        "volume": to_int(kite_quote.get("volume")),
        "open_interest": to_int(kite_quote.get("oi")),

        "trade_date": snapshot_time.date(),
        "snapshot_label": snapshot_label,

        "exchange_timestamp": to_sql_datetime(kite_quote.get("timestamp")),
        "last_trade_time": to_sql_datetime(kite_quote.get("last_trade_time")),
        "last_quantity": to_int(kite_quote.get("last_quantity")),
        "average_price": to_float(kite_quote.get("average_price")),
        "buy_quantity": to_int(kite_quote.get("buy_quantity")),
        "sell_quantity": to_int(kite_quote.get("sell_quantity")),
        "oi_day_high": to_int(kite_quote.get("oi_day_high")),
        "oi_day_low": to_int(kite_quote.get("oi_day_low")),
        "bid_orders": to_int(best_bid.get("orders")),
        "ask_orders": to_int(best_ask.get("orders")),

        "data_source": DATA_SOURCE,
    }


def upsert_option_snapshot_rows(
    db: DatabaseClient,
    rows: list[dict[str, Any]],
    snapshot_table: str = "dbo.OptionSnapshot",
) -> int:
    if not rows:
        return 0

    if getattr(db, "db_kind", "") == "postgres":
        table = pg_table_name(snapshot_table)
        sql = f"""
            INSERT INTO {table}
            (
                option_instrument_id,
                snapshot_time,
                underlying_price,
                last_price,
                bid_price,
                bid_qty,
                ask_price,
                ask_qty,
                volume,
                open_interest,
                trade_date,
                snapshot_label,
                exchange_timestamp,
                last_trade_time,
                last_quantity,
                average_price,
                buy_quantity,
                sell_quantity,
                oi_day_high,
                oi_day_low,
                bid_orders,
                ask_orders,
                data_source
            )
            VALUES
            (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (option_instrument_id, trade_date, snapshot_label)
            DO UPDATE SET
                snapshot_time = EXCLUDED.snapshot_time,
                underlying_price = EXCLUDED.underlying_price,
                last_price = EXCLUDED.last_price,
                bid_price = EXCLUDED.bid_price,
                bid_qty = EXCLUDED.bid_qty,
                ask_price = EXCLUDED.ask_price,
                ask_qty = EXCLUDED.ask_qty,
                volume = EXCLUDED.volume,
                open_interest = EXCLUDED.open_interest,
                exchange_timestamp = EXCLUDED.exchange_timestamp,
                last_trade_time = EXCLUDED.last_trade_time,
                last_quantity = EXCLUDED.last_quantity,
                average_price = EXCLUDED.average_price,
                buy_quantity = EXCLUDED.buy_quantity,
                sell_quantity = EXCLUDED.sell_quantity,
                oi_day_high = EXCLUDED.oi_day_high,
                oi_day_low = EXCLUDED.oi_day_low,
                bid_orders = EXCLUDED.bid_orders,
                ask_orders = EXCLUDED.ask_orders,
                data_source = EXCLUDED.data_source
        """
        values = [
            (
                r["option_instrument_id"],
                r["snapshot_time"],
                r["underlying_price"],
                r["last_price"],
                r["bid_price"],
                r["bid_qty"],
                r["ask_price"],
                r["ask_qty"],
                r["volume"],
                r["open_interest"],
                r["trade_date"],
                r["snapshot_label"],
                r["exchange_timestamp"],
                r["last_trade_time"],
                r["last_quantity"],
                r["average_price"],
                r["buy_quantity"],
                r["sell_quantity"],
                r["oi_day_high"],
                r["oi_day_low"],
                r["bid_orders"],
                r["ask_orders"],
                r["data_source"],
            )
            for r in rows
        ]
        cursor = db.conn.cursor()
        cursor.executemany(sql, values)
        db.conn.commit()
        cursor.close()
        return len(rows)

    table = safe_table_name(snapshot_table)

    sql = f"""
        UPDATE {table}
        SET
            snapshot_time = ?,
            underlying_price = ?,
            last_price = ?,
            bid_price = ?,
            bid_qty = ?,
            ask_price = ?,
            ask_qty = ?,
            volume = ?,
            open_interest = ?,
            exchange_timestamp = ?,
            last_trade_time = ?,
            last_quantity = ?,
            average_price = ?,
            buy_quantity = ?,
            sell_quantity = ?,
            oi_day_high = ?,
            oi_day_low = ?,
            bid_orders = ?,
            ask_orders = ?,
            data_source = ?
        WHERE option_instrument_id = ?
          AND trade_date = ?
          AND snapshot_label = ?;

        IF @@ROWCOUNT = 0
        BEGIN
            INSERT INTO {table}
            (
                option_instrument_id,
                snapshot_time,
                underlying_price,
                last_price,
                bid_price,
                bid_qty,
                ask_price,
                ask_qty,
                volume,
                open_interest,
                trade_date,
                snapshot_label,
                exchange_timestamp,
                last_trade_time,
                last_quantity,
                average_price,
                buy_quantity,
                sell_quantity,
                oi_day_high,
                oi_day_low,
                bid_orders,
                ask_orders,
                data_source
            )
            VALUES
            (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            );
        END
    """

    cursor = db.conn.cursor()
    count = 0

    for r in rows:
        update_params = [
            r["snapshot_time"],
            r["underlying_price"],
            r["last_price"],
            r["bid_price"],
            r["bid_qty"],
            r["ask_price"],
            r["ask_qty"],
            r["volume"],
            r["open_interest"],
            r["exchange_timestamp"],
            r["last_trade_time"],
            r["last_quantity"],
            r["average_price"],
            r["buy_quantity"],
            r["sell_quantity"],
            r["oi_day_high"],
            r["oi_day_low"],
            r["bid_orders"],
            r["ask_orders"],
            r["data_source"],
            r["option_instrument_id"],
            r["trade_date"],
            r["snapshot_label"],
        ]

        insert_params = [
            r["option_instrument_id"],
            r["snapshot_time"],
            r["underlying_price"],
            r["last_price"],
            r["bid_price"],
            r["bid_qty"],
            r["ask_price"],
            r["ask_qty"],
            r["volume"],
            r["open_interest"],
            r["trade_date"],
            r["snapshot_label"],
            r["exchange_timestamp"],
            r["last_trade_time"],
            r["last_quantity"],
            r["average_price"],
            r["buy_quantity"],
            r["sell_quantity"],
            r["oi_day_high"],
            r["oi_day_low"],
            r["bid_orders"],
            r["ask_orders"],
            r["data_source"],
        ]

        cursor.execute(sql, update_params + insert_params)
        count += 1

    db.conn.commit()
    cursor.close()

    return count


def get_option_snapshot_ids(
    db: DatabaseClient,
    rows: list[dict[str, Any]],
    snapshot_table: str = "dbo.OptionSnapshot",
) -> list[int]:
    if not rows:
        return []

    is_postgres = getattr(db, "db_kind", "") == "postgres"
    table = pg_table_name(snapshot_table) if is_postgres else safe_table_name(snapshot_table)
    placeholder = "%s" if is_postgres else "?"

    cursor = db.conn.cursor()
    snapshot_ids: list[int] = []

    for row in rows:
        cursor.execute(
            f"""
            SELECT id
            FROM {table}
            WHERE option_instrument_id = {placeholder}
              AND trade_date = {placeholder}
              AND snapshot_label = {placeholder}
            """,
            (row["option_instrument_id"], row["trade_date"], row["snapshot_label"])
            if is_postgres
            else (row["option_instrument_id"], row["trade_date"], row["snapshot_label"]),
        )
        result = cursor.fetchone()

        if result is not None:
            snapshot_ids.append(int(result[0] if is_postgres else result.id))

    cursor.close()

    return snapshot_ids


def calculate_snapshot_ids_in_batches(
    db: DatabaseClient,
    snapshot_ids: list[int],
    snapshot_table: str,
    instrument_table: str,
    batch_size: int,
) -> dict[str, int]:
    totals = {
        "rows_processed": 0,
        "ok": 0,
        "non_ok": 0,
        "errors": 0,
    }

    if not snapshot_ids:
        return totals

    calc_settings = CalcSettings(
        option_snapshot_table=snapshot_table,
        option_instrument_table=instrument_table,
    )

    for batch in chunked(snapshot_ids, batch_size):
        result = calculate_snapshot_ids(db, calc_settings, batch)

        totals["rows_processed"] += result.get("rows_processed", 0)
        totals["ok"] += result.get("ok", 0)
        totals["non_ok"] += result.get("non_ok", 0)
        totals["errors"] += result.get("errors", 0)

    return totals


def capture_all_option_instruments_snapshot(
    snapshot_label: str | None = None,
    underlying: str = "NIFTY",
    spot_key: str | None = None,
    instrument_table: str = "dbo.OptionInstrument",
    snapshot_table: str = "dbo.OptionSnapshot",
    option_type: str | None = None,
    expiry_to: date | None = None,
    strike_min: float | None = None,
    strike_max: float | None = None,
    max_instruments: int | None = None,
    quote_batch_size: int = 200,
    quote_sleep_seconds: float = 0.20,
    quote_retries: int = DEFAULT_QUOTE_RETRIES,
    quote_retry_sleep_seconds: float = DEFAULT_QUOTE_RETRY_SLEEP_SECONDS,
    skip_calc: bool = False,
    calc_batch_size: int = 500,
    debug_missing_quotes: bool = False,
    schedule_tolerance_seconds: int | None = DEFAULT_SCHEDULE_TOLERANCE_SECONDS,
    allow_outside_window: bool = False,
    snapshot_label_mode: str = SNAPSHOT_LABEL_MODE_SCHEDULED,
) -> dict[str, Any]:
    underlying = underlying.upper()
    manual_snapshot_label = snapshot_label is not None
    run_started_at = now_ist_naive()
    resolved_label, scheduled_target_time, schedule_delta_seconds, within_schedule_tolerance = (
        resolve_scheduled_snapshot_label(
            snapshot_time=run_started_at,
            explicit_label=snapshot_label,
            schedule_tolerance_seconds=schedule_tolerance_seconds,
            allow_outside_window=allow_outside_window,
            snapshot_label_mode=snapshot_label_mode,
        )
    )
    snapshot_label = resolved_label

    settings = get_settings()

    kite_client = KiteClient(settings)
    kite_client.authenticate()

    db = get_database_client(settings)
    db.connect()

    spot: float | None = None
    instruments: list[dict[str, Any]] = []
    quotes: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    missing_quotes: list[str] = []
    upserted = 0
    snapshot_ids: list[int] = []
    calc_result = {
        "rows_processed": 0,
        "ok": 0,
        "non_ok": 0,
        "errors": 0,
    }

    try:
        ensure_snapshot_schema(db, snapshot_table)

        instruments = load_all_live_option_instruments(
            db=db,
            underlying=underlying,
            as_of=run_started_at.date(),
            instrument_table=instrument_table,
            option_type=option_type,
            expiry_to=expiry_to,
            strike_min=strike_min,
            strike_max=strike_max,
            max_instruments=max_instruments,
        )

        if not instruments:
            raise RuntimeError(
                f"No live option instruments found for {underlying} "
                f"as_of={run_started_at.date()}"
            )

        snapshot_time = now_ist_naive()
        if snapshot_label_mode == SNAPSHOT_LABEL_MODE_5M and not manual_snapshot_label:
            snapshot_label, scheduled_target_time, schedule_delta_seconds, within_schedule_tolerance = (
                resolve_scheduled_snapshot_label(
                    snapshot_time=snapshot_time,
                    explicit_label=None,
                    schedule_tolerance_seconds=schedule_tolerance_seconds,
                    allow_outside_window=allow_outside_window,
                    snapshot_label_mode=snapshot_label_mode,
                )
            )
        elif snapshot_label in SCHEDULED_SNAPSHOTS:
            snapshot_label, scheduled_target_time, schedule_delta_seconds, within_schedule_tolerance = (
                resolve_scheduled_snapshot_label(
                    snapshot_time=snapshot_time,
                    explicit_label=snapshot_label,
                    schedule_tolerance_seconds=schedule_tolerance_seconds,
                    allow_outside_window=allow_outside_window,
                    snapshot_label_mode=snapshot_label_mode,
                )
            )
        spot = get_spot_quote(
            kite_client=kite_client,
            underlying=underlying,
            spot_key=spot_key,
            max_retries=quote_retries,
            retry_sleep_seconds=quote_retry_sleep_seconds,
        )

        print(
            f"START live quote capture | {underlying} | {snapshot_label} | "
            f"spot={spot:.2f} | instruments={len(instruments)} | "
            f"actual_ist={snapshot_time:%Y-%m-%d %H:%M:%S} | "
            f"scheduled_target_ist={scheduled_target_time or 'manual'} | "
            f"delta_seconds={schedule_delta_seconds}"
        )

        quotes = fetch_option_quotes(
            kite_client=kite_client,
            instruments=instruments,
            quote_batch_size=quote_batch_size,
            sleep_seconds=quote_sleep_seconds,
            max_retries=quote_retries,
            retry_sleep_seconds=quote_retry_sleep_seconds,
        )

        for inst in instruments:
            key = f"{inst['exchange']}:{inst['tradingsymbol']}"
            q = quotes.get(key)

            if not q:
                missing_quotes.append(key)

                if debug_missing_quotes:
                    print(
                        "MISSING_QUOTE | "
                        f"{key} | token={inst['instrument_token']} | "
                        f"expiry={inst['expiry']} | strike={inst['strike']} | "
                        f"type={inst['instrument_type']}"
                    )

                continue

            rows.append(
                build_snapshot_row(
                    instrument=inst,
                    kite_quote=q,
                    underlying_price=spot,
                    snapshot_time=snapshot_time,
                    snapshot_label=snapshot_label,
                )
            )

        upserted = upsert_option_snapshot_rows(
            db=db,
            rows=rows,
            snapshot_table=snapshot_table,
        )

        snapshot_ids = get_option_snapshot_ids(
            db=db,
            rows=rows,
            snapshot_table=snapshot_table,
        )

        if not skip_calc and snapshot_ids:
            calc_result = calculate_snapshot_ids_in_batches(
                db=db,
                snapshot_ids=snapshot_ids,
                snapshot_table=snapshot_table,
                instrument_table=instrument_table,
                batch_size=calc_batch_size,
            )

    finally:
        db.close()

    result = {
        "underlying": underlying,
        "snapshot_label": snapshot_label,
        "snapshot_time": snapshot_time.isoformat(sep=" "),
        "scheduled_target_time_ist": scheduled_target_time,
        "schedule_delta_seconds": schedule_delta_seconds,
        "within_schedule_tolerance": within_schedule_tolerance,
        "snapshot_label_mode": snapshot_label_mode,
        "spot": spot,
        "mode": "ALL_LIVE_OPTION_INSTRUMENTS_NO_CHAIN_SELECTION",
        "option_type": option_type or "CE + PE",
        "expiry_to": str(expiry_to) if expiry_to else None,
        "strike_min": strike_min,
        "strike_max": strike_max,
        "max_instruments": max_instruments,
        "instruments_loaded": len(instruments),
        "quotes_received": len(quotes),
        "rows_built": len(rows),
        "rows_upserted": upserted,
        "snapshot_ids": len(snapshot_ids),
        "calc_rows_processed": calc_result["rows_processed"],
        "calc_ok": calc_result["ok"],
        "calc_non_ok": calc_result["non_ok"],
        "calc_errors": calc_result["errors"],
        "missing_quotes_count": len(missing_quotes),
        "missing_quotes": missing_quotes[:50],
        "data_source": DATA_SOURCE,
    }

    print("Snapshot capture completed")
    for key, value in result.items():
        print(f"{key}: {value}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture live quote snapshots for ALL option instruments of an underlying."
    )

    parser.add_argument(
        "--snapshot-label",
        default=None,
        help=(
            "Optional manual snapshot label. If omitted, the script infers the "
            "label from --snapshot-label-mode."
        ),
    )
    parser.add_argument(
        "--snapshot-label-mode",
        choices=(SNAPSHOT_LABEL_MODE_SCHEDULED, SNAPSHOT_LABEL_MODE_5M),
        default=SNAPSHOT_LABEL_MODE_SCHEDULED,
        help="scheduled keeps OPEN_0915/CLOSE_1515 behavior; m5 writes stable M5_HHMM labels for 5-minute cron runs.",
    )

    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--spot-key", default=None, help="Optional Kite quote key, e.g. NSE:NIFTY 50")

    parser.add_argument("--instrument-table", default="dbo.OptionInstrument")
    parser.add_argument("--snapshot-table", default="dbo.OptionSnapshot")

    parser.add_argument("--option-type", choices=["CE", "PE"], default=None)

    parser.add_argument(
        "--expiry-to",
        default=None,
        help="Optional expiry upper bound YYYY-MM-DD. Useful to avoid quoting far expiries.",
    )

    parser.add_argument("--strike-min", type=float, default=None)
    parser.add_argument("--strike-max", type=float, default=None)

    parser.add_argument(
        "--max-instruments",
        type=int,
        default=None,
        help="Optional safety cap for testing. Do not use for full production capture.",
    )

    parser.add_argument("--quote-batch-size", type=int, default=200)
    parser.add_argument("--quote-sleep-seconds", type=float, default=0.20)
    parser.add_argument(
        "--quote-retries",
        type=int,
        default=DEFAULT_QUOTE_RETRIES,
        help=f"Kite quote retry count after the first attempt. Default: {DEFAULT_QUOTE_RETRIES}",
    )
    parser.add_argument(
        "--quote-retry-sleep-seconds",
        type=float,
        default=DEFAULT_QUOTE_RETRY_SLEEP_SECONDS,
        help=(
            "Base backoff seconds for Kite quote retries. "
            f"Default: {DEFAULT_QUOTE_RETRY_SLEEP_SECONDS}"
        ),
    )

    parser.add_argument("--skip-calc", action="store_true")
    parser.add_argument("--calc-batch-size", type=int, default=500)

    parser.add_argument("--debug-missing-quotes", action="store_true")
    parser.add_argument(
        "--schedule-tolerance-seconds",
        type=int,
        default=DEFAULT_SCHEDULE_TOLERANCE_SECONDS,
        help=(
            "Maximum seconds away from the scheduled IST time before the run is "
            "rejected. Default: 300. Use a negative value to disable validation."
        ),
    )
    parser.add_argument(
        "--allow-outside-window",
        action="store_true",
        help="Allow writing even when the scheduled job fires outside the tolerance window.",
    )

    args = parser.parse_args()
    schedule_tolerance_seconds = (
        None if args.schedule_tolerance_seconds < 0 else args.schedule_tolerance_seconds
    )

    capture_all_option_instruments_snapshot(
        snapshot_label=args.snapshot_label,
        underlying=args.underlying,
        spot_key=args.spot_key,
        instrument_table=args.instrument_table,
        snapshot_table=args.snapshot_table,
        option_type=args.option_type,
        expiry_to=date.fromisoformat(args.expiry_to) if args.expiry_to else None,
        strike_min=args.strike_min,
        strike_max=args.strike_max,
        max_instruments=args.max_instruments,
        quote_batch_size=args.quote_batch_size,
        quote_sleep_seconds=args.quote_sleep_seconds,
        quote_retries=args.quote_retries,
        quote_retry_sleep_seconds=args.quote_retry_sleep_seconds,
        skip_calc=args.skip_calc,
        calc_batch_size=args.calc_batch_size,
        debug_missing_quotes=args.debug_missing_quotes,
        schedule_tolerance_seconds=schedule_tolerance_seconds,
        allow_outside_window=args.allow_outside_window,
        snapshot_label_mode=args.snapshot_label_mode,
    )


if __name__ == "__main__":
    main()
