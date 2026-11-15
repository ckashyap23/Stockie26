from __future__ import annotations


import pyodbc
import pandas as pd
from dotenv import load_dotenv

from technical_analysis_service.common.config import get_azure_sql_conn_str_variants, get_settings
from technical_analysis_service.data_manager.db.client_factory import get_database_client

load_dotenv()


def get_db_connection() -> pyodbc.Connection:
    settings = get_settings()
    conn_str = settings.azure_sql_conn_str
    if not conn_str:
        raise ValueError(
            "AZURE_SQL_CONN_STR is not set in environment or .env file. "
            "Please set it in your .env file."
        )
    last_error: Exception | None = None
    for candidate in get_azure_sql_conn_str_variants(conn_str):
        try:
            return pyodbc.connect(candidate, timeout=10)
        except pyodbc.Error as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError("No valid Azure SQL connection string variants could be generated.")


def get_active_underlyings(instrument_type: str | None = "INDEX") -> list[str]:
    """Return tradingsymbols of active watched instruments from WatchedInstrument."""
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        watched = db.get_watched_instruments(instrument_type=instrument_type)
        return [w.tradingsymbol for w in watched if w.is_active]
    finally:
        db.close()


def fetch_index_daily(
    conn,
    snapshot_table: str | None = None,
    activity_table: str | None = None,
    underlying: str = "NIFTY",
    start_date: str | None = None,
    end_date: str | None = None,
    join_activity: bool = True,
) -> pd.DataFrame:
    """Fetch daily index rows. Works with both pyodbc (Azure SQL) and psycopg2 (Supabase)."""
    is_postgres = hasattr(conn, "cursor") and not hasattr(conn, "autocommit")
    try:
        import psycopg2
        is_postgres = isinstance(conn, psycopg2.extensions.connection)
    except ImportError:
        pass

    ph = "%s" if is_postgres else "?"
    if snapshot_table is None:
        snapshot_table = '"UnderlyingSnapshot"' if is_postgres else "dbo.UnderlyingSnapshot"
    if activity_table is None:
        activity_table = '"MarketActivityDaily"' if is_postgres else "dbo.MarketActivityDaily"

    where = [f"s.underlying = {ph}"]
    params: list[object] = [underlying]

    if start_date:
        where.append(f"s.trade_date >= {ph}")
        params.append(pd.to_datetime(start_date).date())

    if end_date:
        where.append(f"s.trade_date <= {ph}")
        params.append(pd.to_datetime(end_date).date())

    where_clause = " AND ".join(where)

    # Supabase doesn't have MarketActivityDaily — fall back to no-join
    if join_activity and not is_postgres:
        sql = f"""
        SELECT
            s.trade_date, s.open_price, s.high_price, s.low_price, s.close_price, s.volume,
            m.expiry_date AS fut_expiry_date, m.close_price AS fut_close_price,
            m.settle_price AS fut_settle_price, m.underlying_price AS fut_underlying_price,
            m.open_interest AS fut_open_interest, m.change_in_oi AS fut_change_in_oi,
            m.traded_volume AS fut_traded_volume, m.traded_value AS fut_traded_value
        FROM {snapshot_table} s
        LEFT JOIN {activity_table} m
          ON m.underlying = s.underlying AND m.trade_date = s.trade_date
         AND m.fin_instrm_tp = 'IDF' AND m.tckr_symb = s.underlying
        WHERE {where_clause}
        ORDER BY s.trade_date
        """
    else:
        sql = f"""
        SELECT s.trade_date, s.open_price, s.high_price, s.low_price, s.close_price, s.volume
        FROM {snapshot_table} s
        WHERE {where_clause}
        ORDER BY s.trade_date
        """

    df = pd.read_sql(sql, conn, params=params)
    if df.empty:
        return df

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)

    if "close_price" not in df.columns:
        raise ValueError(f"{snapshot_table} must contain close_price for predictions.")

    return df


def fetch_5m_candles_for_dates(
    conn,
    underlying: str,
    dates: list,
) -> pd.DataFrame:
    """Fetch aggregated min low / max high from UnderlyingCandle5m for given dates."""
    if not dates:
        return pd.DataFrame()

    date_list = [
        pd.to_datetime(d).date() if not isinstance(d, type(pd.Timestamp.now().date())) else d
        for d in dates
    ]
    unique_dates = sorted(set(date_list))

    if not unique_dates:
        return pd.DataFrame()

    is_postgres = False
    try:
        import psycopg2
        is_postgres = isinstance(conn, psycopg2.extensions.connection)
    except ImportError:
        pass

    ph = "%s" if is_postgres else "?"
    table = '"UnderlyingCandle5m"' if is_postgres else "dbo.UnderlyingCandle5m"
    placeholders = ",".join([ph for _ in unique_dates])
    sql = f"""
        SELECT
            trade_date,
            MIN(low_price) AS min_low_price,
            MAX(high_price) AS max_high_price
        FROM {table}
        WHERE underlying = {ph}
          AND trade_date IN ({placeholders})
        GROUP BY trade_date
        ORDER BY trade_date
    """

    params = [underlying] + unique_dates
    df = pd.read_sql(sql, conn, params=params)

    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()

    return df


if __name__ == "__main__":
    conn = get_db_connection()
    try:
        df = fetch_index_daily(
            conn,
            underlying="NIFTY",
            start_date="2025-01-01",
            end_date="2025-01-31",
            join_activity=True,
        )
        print(df.head(10))
        print(df.tail(5))
    finally:
        conn.close()
