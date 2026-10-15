from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import psycopg2
from sqlalchemy import BIGINT, Column, Date, DateTime, MetaData, Numeric, String, Table
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from src.common.config import get_settings


metadata = MetaData()

global_index_ohlc = Table(
    "global_index_ohlc",
    metadata,
    Column("trade_date", Date, primary_key=True, nullable=False),
    Column("index_name", String(100), nullable=False),
    Column("symbol", String(32), primary_key=True, nullable=False),
    Column("open", Numeric(18, 4)),
    Column("high", Numeric(18, 4)),
    Column("low", Numeric(18, 4)),
    Column("close", Numeric(18, 4)),
    Column("volume", BIGINT),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Column("updated_at", DateTime, server_default=func.current_timestamp()),
)


def create_postgres_engine() -> Engine:
    settings = get_settings()
    conn_str = settings.supabase_conn_str
    if not conn_str:
        raise RuntimeError("SUPABASE_CONN_STR is missing in .env")

    return create_engine(
        "postgresql+psycopg2://",
        creator=lambda: psycopg2.connect(conn_str),
        pool_pre_ping=True,
    )


def create_index_data_table(engine: Engine) -> None:
    metadata.create_all(engine, tables=[global_index_ohlc])


def prepare_index_data_records(index_df: pd.DataFrame) -> list[dict]:
    if index_df.empty:
        return []

    prepared_df = index_df.copy()
    prepared_df["date"] = pd.to_datetime(prepared_df["date"]).dt.date
    prepared_df = prepared_df.rename(columns={"date": "trade_date"})

    return prepared_df[
        [
            "trade_date",
            "index_name",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ].to_dict(orient="records")


def upsert_index_data(engine: Engine, index_df: pd.DataFrame) -> int:
    records = prepare_index_data_records(index_df)
    if not records:
        return 0

    insert_stmt = insert(global_index_ohlc).values(records)
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=["trade_date", "symbol"],
        set_={
            "index_name": insert_stmt.excluded.index_name,
            "open": insert_stmt.excluded.open,
            "high": insert_stmt.excluded.high,
            "low": insert_stmt.excluded.low,
            "close": insert_stmt.excluded.close,
            "volume": insert_stmt.excluded.volume,
            "updated_at": func.current_timestamp(),
        },
    )

    with engine.begin() as connection:
        connection.execute(upsert_stmt)

    return len(records)


def get_latest_trade_date(engine: Engine) -> date | None:
    stmt = select(func.max(global_index_ohlc.c.trade_date))
    with engine.begin() as connection:
        return connection.execute(stmt).scalar_one_or_none()


def date_has_incomplete_rows(engine: Engine, trade_date: date) -> bool:
    stmt = (
        select(func.count())
        .select_from(global_index_ohlc)
        .where(global_index_ohlc.c.trade_date == trade_date)
        .where(
            (global_index_ohlc.c.index_name.is_(None))
            | (global_index_ohlc.c.symbol.is_(None))
            | (global_index_ohlc.c.open.is_(None))
            | (global_index_ohlc.c.high.is_(None))
            | (global_index_ohlc.c.low.is_(None))
            | (global_index_ohlc.c.close.is_(None))
            | (global_index_ohlc.c.volume.is_(None))
        )
    )
    with engine.begin() as connection:
        missing_count = connection.execute(stmt).scalar_one()
    return missing_count > 0


def resolve_incremental_start_date(
    engine: Engine,
    fallback_start_date: date,
) -> date:
    latest_trade_date = get_latest_trade_date(engine)
    if latest_trade_date is None:
        return fallback_start_date

    if date_has_incomplete_rows(engine, latest_trade_date):
        return latest_trade_date

    return latest_trade_date + timedelta(days=1)
