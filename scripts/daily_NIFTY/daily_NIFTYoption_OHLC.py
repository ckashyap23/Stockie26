# scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py
"""
Capture live option quote OHLC for NIFTY option instruments into OptionOhlc.

This stores current-day quote OHLC separately from OptionSnapshot and
OptionSnapshotCalc. It uses Kite quote(), so the OHLC is the current trading
day's live quote OHLC state rather than a requested historical candle.

Usage:
    python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py
    python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --trade-date 2026-06-26 --max-instruments 25
"""

from __future__ import annotations

import argparse
import re
import sys
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime
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


load_dotenv(project_root / ".env")

IST = ZoneInfo("Asia/Kolkata")
DATA_SOURCE = "KITE_QUOTE_LIVE_OHLC"
DEFAULT_UNDERLYING = "NIFTY"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class DailyOptionOhlcSettings:
    option_instrument_table: str = "dbo.OptionInstrument"
    option_ohlc_table: str = "dbo.OptionOhlc"
    quote_batch_size: int = 200
    sleep_seconds: float = 0.25


def now_ist_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def safe_table_name(name: str) -> str:
    parts = name.split(".")
    if len(parts) not in (1, 2):
        raise ValueError(f"Invalid table name: {name}")
    for part in parts:
        if not _IDENTIFIER_RE.match(part):
            raise ValueError(f"Invalid table identifier: {part}")
    return ".".join(f"[{part}]" for part in parts)


def pg_table_name(name: str) -> str:
    short_name = name.split(".")[-1]
    if not _IDENTIFIER_RE.match(short_name):
        raise ValueError(f"Invalid table identifier: {short_name}")
    return f'"{short_name}"'


def table_object_name(name: str) -> str:
    return name if "." in name else f"dbo.{name}"


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(IST).replace(tzinfo=None)
        return value
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        return parsed.astimezone(IST).replace(tzinfo=None)
    return parsed


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def chunked(items: list[Any], chunk_size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), chunk_size):
        yield items[i:i + chunk_size]


def ensure_option_ohlc_schema(db: DatabaseClient, settings: DailyOptionOhlcSettings) -> None:
    if getattr(db, "db_kind", "") == "postgres":
        table = pg_table_name(settings.option_ohlc_table)
        instrument_table = pg_table_name(settings.option_instrument_table)
        cursor = db.conn.cursor()
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id bigserial PRIMARY KEY,
                option_instrument_id bigint NOT NULL REFERENCES {instrument_table}(id),
                underlying varchar(50) NOT NULL,
                trade_date date NOT NULL,
                candle_time timestamp NOT NULL,
                candle_interval varchar(20) NOT NULL,
                open_price double precision,
                high_price double precision,
                low_price double precision,
                close_price double precision,
                volume bigint,
                open_interest bigint,
                last_price double precision,
                exchange_timestamp timestamp,
                data_source varchar(50) NOT NULL,
                loaded_at timestamp NOT NULL,
                CONSTRAINT uq_option_ohlc_instrument_date_interval_source
                    UNIQUE (option_instrument_id, trade_date, candle_interval, data_source)
            );
            CREATE INDEX IF NOT EXISTS ix_option_ohlc_underlying_date
                ON {table} (underlying, trade_date);
            CREATE INDEX IF NOT EXISTS ix_option_ohlc_date_source
                ON {table} (trade_date, data_source);
            """
        )
        db.conn.commit()
        cursor.close()
        return

    table = safe_table_name(settings.option_ohlc_table)
    full_name = table_object_name(settings.option_ohlc_table)
    cursor = db.conn.cursor()
    cursor.execute(
        f"""
        IF OBJECT_ID('{full_name}', 'U') IS NULL
        BEGIN
            CREATE TABLE {table} (
                id BIGINT IDENTITY(1,1) PRIMARY KEY,
                option_instrument_id BIGINT NOT NULL,
                underlying VARCHAR(50) NOT NULL,
                trade_date DATE NOT NULL,
                candle_time DATETIME2 NOT NULL,
                candle_interval VARCHAR(20) NOT NULL,
                open_price FLOAT NULL,
                high_price FLOAT NULL,
                low_price FLOAT NULL,
                close_price FLOAT NULL,
                volume BIGINT NULL,
                open_interest BIGINT NULL,
                last_price FLOAT NULL,
                exchange_timestamp DATETIME2 NULL,
                data_source VARCHAR(50) NOT NULL,
                loaded_at DATETIME2 NOT NULL
            );
            CREATE UNIQUE INDEX UX_OptionOhlc_Instrument_Date_Interval_Source
                ON {table}(option_instrument_id, trade_date, candle_interval, data_source);
            CREATE INDEX IX_OptionOhlc_Underlying_Date
                ON {table}(underlying, trade_date);
            CREATE INDEX IX_OptionOhlc_Date_Source
                ON {table}(trade_date, data_source);
        END;
        """
    )
    db.conn.commit()
    cursor.close()


def load_live_option_instruments(
    db: DatabaseClient,
    settings: DailyOptionOhlcSettings,
    underlying: str,
    trade_date: date,
    option_type: str | None = None,
    expiry_to: date | None = None,
    strike_min: float | None = None,
    strike_max: float | None = None,
    max_instruments: int | None = None,
) -> list[dict[str, Any]]:
    is_postgres = getattr(db, "db_kind", "") == "postgres"
    table = pg_table_name(settings.option_instrument_table) if is_postgres else safe_table_name(settings.option_instrument_table)
    placeholder = "%s" if is_postgres else "?"
    expiry_expr = "expiry" if is_postgres else "CAST(expiry AS date)"

    sql = f"""
        SELECT id, instrument_token, exchange, underlying, tradingsymbol,
               strike, {expiry_expr} AS expiry, instrument_type
        FROM {table}
        WHERE underlying = {placeholder}
          AND {expiry_expr} >= {placeholder}
          AND instrument_token IS NOT NULL
          AND instrument_type IN ('CE', 'PE')
    """
    params: list[Any] = [underlying.upper(), trade_date]

    if expiry_to:
        sql += f" AND {expiry_expr} <= {placeholder}"
        params.append(expiry_to)
    if option_type:
        sql += f" AND instrument_type = {placeholder}"
        params.append(option_type.upper())
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
            "instrument_token": int(token),
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
    batch_size: int,
    sleep_seconds: float,
) -> dict[str, dict[str, Any]]:
    quote_keys = [
        f"{instrument['exchange']}:{instrument['tradingsymbol']}"
        for instrument in instruments
        if instrument.get("exchange") and instrument.get("tradingsymbol")
    ]
    quotes: dict[str, dict[str, Any]] = {}
    for keys in chunked(quote_keys, batch_size):
        quotes.update(kite_client.kite.quote(keys))
        if sleep_seconds > 0:
            time_module.sleep(sleep_seconds)
    return quotes


def quote_to_ohlc_row(
    instrument: dict[str, Any],
    quote: dict[str, Any],
    trade_date: date,
    loaded_at: datetime,
) -> dict[str, Any] | None:
    ohlc = quote.get("ohlc") or {}
    if not ohlc:
        return None

    exchange_timestamp = _to_datetime(quote.get("timestamp"))
    candle_time = exchange_timestamp or loaded_at
    return {
        "option_instrument_id": instrument["id"],
        "underlying": instrument["underlying"],
        "trade_date": trade_date,
        "candle_time": candle_time,
        "candle_interval": "day",
        "open_price": _to_float(ohlc.get("open")),
        "high_price": _to_float(ohlc.get("high")),
        "low_price": _to_float(ohlc.get("low")),
        "close_price": _to_float(ohlc.get("close")),
        "volume": _to_int(quote.get("volume")),
        "open_interest": _to_int(quote.get("oi")),
        "last_price": _to_float(quote.get("last_price")),
        "exchange_timestamp": exchange_timestamp,
        "data_source": DATA_SOURCE,
        "loaded_at": loaded_at,
    }


def upsert_option_ohlc_rows(
    db: DatabaseClient,
    settings: DailyOptionOhlcSettings,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0

    is_postgres = getattr(db, "db_kind", "") == "postgres"
    table = pg_table_name(settings.option_ohlc_table) if is_postgres else safe_table_name(settings.option_ohlc_table)
    placeholder = "%s" if is_postgres else "?"
    columns = [
        "option_instrument_id", "underlying", "trade_date", "candle_time", "candle_interval",
        "open_price", "high_price", "low_price", "close_price", "volume", "open_interest",
        "last_price", "exchange_timestamp", "data_source", "loaded_at",
    ]
    values = [tuple(row[col] for col in columns) for row in rows]

    if is_postgres:
        sql = f"""
            INSERT INTO {table}
                ({', '.join(columns)})
            VALUES ({', '.join([placeholder] * len(columns))})
            ON CONFLICT (option_instrument_id, trade_date, candle_interval, data_source)
            DO UPDATE SET
                underlying = EXCLUDED.underlying,
                candle_time = EXCLUDED.candle_time,
                open_price = EXCLUDED.open_price,
                high_price = EXCLUDED.high_price,
                low_price = EXCLUDED.low_price,
                close_price = EXCLUDED.close_price,
                volume = EXCLUDED.volume,
                open_interest = EXCLUDED.open_interest,
                last_price = EXCLUDED.last_price,
                exchange_timestamp = EXCLUDED.exchange_timestamp,
                loaded_at = EXCLUDED.loaded_at
        """
        cursor = db.conn.cursor()
        cursor.executemany(sql, values)
        db.conn.commit()
        cursor.close()
        return len(rows)

    cursor = db.conn.cursor()
    cursor.fast_executemany = True
    update_sql = f"""
        UPDATE {table}
        SET underlying = ?, candle_time = ?, open_price = ?, high_price = ?,
            low_price = ?, close_price = ?, volume = ?, open_interest = ?,
            last_price = ?, exchange_timestamp = ?, loaded_at = ?
        WHERE option_instrument_id = ?
          AND trade_date = ?
          AND candle_interval = ?
          AND data_source = ?;

        IF @@ROWCOUNT = 0
        BEGIN
            INSERT INTO {table}
                ({', '.join(columns)})
            VALUES ({', '.join(['?'] * len(columns))});
        END
    """
    params = []
    for row in rows:
        update_values = [
            row["underlying"], row["candle_time"], row["open_price"], row["high_price"],
            row["low_price"], row["close_price"], row["volume"], row["open_interest"],
            row["last_price"], row["exchange_timestamp"], row["loaded_at"],
            row["option_instrument_id"], row["trade_date"], row["candle_interval"], row["data_source"],
        ]
        insert_values = [row[col] for col in columns]
        params.append(tuple(update_values + insert_values))
    cursor.executemany(update_sql, params)
    db.conn.commit()
    cursor.close()
    return len(rows)


def run_daily_option_ohlc(
    trade_date: date | None = None,
    underlying: str = DEFAULT_UNDERLYING,
    option_type: str | None = None,
    expiry_to: date | None = None,
    strike_min: float | None = None,
    strike_max: float | None = None,
    max_instruments: int | None = None,
    dry_run: bool = False,
    settings: DailyOptionOhlcSettings | None = None,
) -> dict[str, int]:
    settings = settings or DailyOptionOhlcSettings()
    app_settings = get_settings()
    snapshot_time = now_ist_naive()
    trade_date = trade_date or snapshot_time.date()

    db = get_database_client(app_settings)
    db.connect()
    try:
        ensure_option_ohlc_schema(db, settings)
        instruments = load_live_option_instruments(
            db=db,
            settings=settings,
            underlying=underlying,
            trade_date=trade_date,
            option_type=option_type,
            expiry_to=expiry_to,
            strike_min=strike_min,
            strike_max=strike_max,
            max_instruments=max_instruments,
        )
    finally:
        db.close()

    print(f"Loaded {len(instruments):,} live {underlying.upper()} option instruments for {trade_date}.")
    if dry_run:
        return {"instruments": len(instruments), "quotes": 0, "rows_prepared": 0, "rows_upserted": 0}

    kite_client = KiteClient(app_settings)
    kite_client.authenticate()
    quotes = fetch_option_quotes(
        kite_client=kite_client,
        instruments=instruments,
        batch_size=settings.quote_batch_size,
        sleep_seconds=settings.sleep_seconds,
    )

    loaded_at = now_ist_naive()
    rows: list[dict[str, Any]] = []
    for instrument in instruments:
        key = f"{instrument['exchange']}:{instrument['tradingsymbol']}"
        quote = quotes.get(key)
        if not quote:
            continue
        row = quote_to_ohlc_row(instrument, quote, trade_date, loaded_at)
        if row:
            rows.append(row)

    db = get_database_client(app_settings)
    db.connect()
    try:
        ensure_option_ohlc_schema(db, settings)
        rows_upserted = upsert_option_ohlc_rows(db, settings, rows)
    finally:
        db.close()

    result = {
        "instruments": len(instruments),
        "quotes": len(quotes),
        "rows_prepared": len(rows),
        "rows_upserted": rows_upserted,
    }
    print(f"Daily option OHLC completed: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture live NIFTY option quote OHLC into OptionOhlc.")
    parser.add_argument("--trade-date", help="YYYY-MM-DD. Defaults to current IST date.")
    parser.add_argument("--underlying", default=DEFAULT_UNDERLYING)
    parser.add_argument("--option-type", choices=["CE", "PE"])
    parser.add_argument("--expiry-to", help="YYYY-MM-DD")
    parser.add_argument("--strike-min", type=float)
    parser.add_argument("--strike-max", type=float)
    parser.add_argument("--max-instruments", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quote-batch-size", type=int, default=200)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    args = parser.parse_args()

    trade_date_val = date.fromisoformat(args.trade_date) if args.trade_date else None
    run_daily_option_ohlc(
        trade_date=trade_date_val,
        underlying=args.underlying.strip().upper(),
        option_type=args.option_type,
        expiry_to=date.fromisoformat(args.expiry_to) if args.expiry_to else None,
        strike_min=args.strike_min,
        strike_max=args.strike_max,
        max_instruments=args.max_instruments,
        dry_run=args.dry_run,
        settings=DailyOptionOhlcSettings(
            quote_batch_size=args.quote_batch_size,
            sleep_seconds=args.sleep_seconds,
        ),
    )

    if args.dry_run:
        print("Dry-run: skipping downstream pipeline chain.")
        return

    # ── Chain: option selections + production PnL backtest ───────────────────
    # signal_date for a trade executed on trade_date D is D-1.  Run the
    # production pipelines over a 30-day rolling window so the upsert is fast
    # while still covering recent predictions and paper-trade PnL.
    import subprocess
    from datetime import timedelta
    resolved_trade_date = trade_date_val or date.today()
    pipe_end   = (resolved_trade_date - timedelta(days=1)).isoformat()  # last signal_date
    pipe_start = (resolved_trade_date - timedelta(days=30)).isoformat()

    steps = [
        ("option-selections",
         [sys.executable,
          str(project_root / "backtest" / "production" / "pipeline_upsert_option_selections.py"),
          "--start", pipe_start, "--end", pipe_end]),
        ("backtest-pnl",
         [sys.executable,
          str(project_root / "backtest" / "production" / "pipeline_backtest_pnl.py"),
          "--start", pipe_start, "--end", pipe_end]),
    ]
    for step_name, cmd in steps:
        print(f"\n── Chaining {step_name} {pipe_start} → {pipe_end} ──")
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_root))
        print((proc.stdout or "").strip()[-2000:])
        if proc.returncode != 0:
            print(f"[WARN] {step_name} exited {proc.returncode}:\n"
                  f"{(proc.stderr or '').strip()[-800:]}")
        else:
            print(f"{step_name} complete.")

    # ── Chain: 5-min option snapshot for today's selected instrument only ─────
    # If a position was taken today, backfill intraday 5-min candles so the
    # PnL simulation can find M5_0930 as entry and apply accurate stop-loss logic.
    print(f"\n── Checking today's option selection for 5-min backfill ──")
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(project_root / ".env")
        from src.common.config import get_settings as _get_settings
        import psycopg2 as _psycopg2
        _s = _get_settings()
        _signal_date = (resolved_trade_date - timedelta(days=1)).isoformat()
        with _psycopg2.connect(_s.supabase_conn_str) as _conn:
            with _conn.cursor() as _cur:
                _cur.execute(
                    'SELECT primary_buy_symbol FROM "NiftyOptionSelection"'
                    " WHERE trade_date = %s AND model_version = 'cascade_v1'"
                    " AND primary_buy_symbol IS NOT NULL LIMIT 1",
                    (_signal_date,),
                )
                _row = _cur.fetchone()
        if _row:
            _opt_symbol = _row[0]
            print(f"  Selected option for {_signal_date}: {_opt_symbol} — fetching 5-min candles for {resolved_trade_date}")
            _backfill_cmd = [
                sys.executable,
                str(project_root / "scripts" / "backfill_NIFTY" / "backfill_option_5m_by_symbol.py"),
                "--start", _signal_date,
                "--end", _signal_date,
                "--symbol", _opt_symbol,
            ]
            _proc = subprocess.run(_backfill_cmd, capture_output=True, text=True, cwd=str(project_root))
            print((_proc.stdout or "").strip()[-1000:])
            if _proc.returncode != 0:
                print(f"[WARN] 5-min option backfill exited {_proc.returncode}:\n"
                      f"{(_proc.stderr or '').strip()[-400:]}")
            else:
                print("5-min option backfill complete.")
        else:
            print(f"  No option selection found for signal_date={_signal_date} — skipping 5-min backfill.")
    except Exception as _exc:
        print(f"[WARN] 5-min option backfill step failed: {_exc}")


if __name__ == "__main__":
    main()