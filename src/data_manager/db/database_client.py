# src/data_manager/db/database_client.py
import logging
from datetime import datetime, date
from typing import Iterable, List, Optional, Dict, Any
import pyodbc

logger = logging.getLogger(__name__)

from src.common.config import Settings, get_azure_sql_conn_str_variants
from src.common.models import StockInstrument, OptionInstrument, OptionData, WatchedInstrument


class DatabaseClient:
    def __init__(self, settings: Settings) -> None:
        self._conn_str = settings.azure_sql_conn_str
        self._conn: Optional[pyodbc.Connection] = None

        if not self._conn_str:
            raise RuntimeError(
                "AZURE_SQL_CONN_STR is missing in .env file.\n"
                "Format: DRIVER={SQL Server};SERVER=server.database.windows.net,1433;DATABASE=mydb;UID=username;PWD=password"
            )

    def connect(self) -> None:
        if self._conn is None:
            try:
                last_error: pyodbc.Error | None = None
                for candidate in get_azure_sql_conn_str_variants(self._conn_str):
                    try:
                        self._conn = pyodbc.connect(candidate, timeout=10)
                        self._conn.autocommit = True
                        return
                    except pyodbc.Error as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error
                raise RuntimeError("No valid Azure SQL connection string variants could be generated.")
            except pyodbc.Error as e:
                error_msg = str(e)
                suggestions = []

                if "IM002" in error_msg or "Data source name not found" in error_msg:
                    suggestions.append(
                        "1. Ensure the correct ODBC driver is installed (ODBC Driver 18 or 17 for SQL Server)"
                    )
                    suggestions.append(
                        "2. Ensure your connection string starts with: Driver={ODBC Driver 18 for SQL Server};"
                    )

                if "Invalid connection string attribute" in error_msg:
                    suggestions.append(
                        "1. Your connection string has a malformed key/value pair. Use ';' to separate attributes."
                    )
                    suggestions.append(
                        "2. Ensure you have: Driver=...;Server=...;Database=...;Uid=...;Pwd=...;Encrypt=yes;TrustServerCertificate=no"
                    )
                    suggestions.append(
                        "3. Avoid wrapping the password in braces unless it contains semicolons."
                    )
                    suggestions.append(
                        "4. If ODBC Driver 18 TLS negotiation fails on this machine, try TrustServerCertificate=yes."
                    )

                if "Login failed for user" in error_msg or "18456" in error_msg or "28000" in error_msg:
                    suggestions.append("1. Verify username and password are correct")
                    suggestions.append(
                        "2. For Azure SQL, the username is often 'user@server' (e.g. kaycee@options-sql-server)"
                    )
                    suggestions.append(
                        "3. Ensure your client IP is allowed in Azure SQL Server firewall rules"
                    )

                if "does not exist" in error_msg or "access denied" in error_msg:
                    suggestions.append(
                        "1. Verify the server name is correct (e.g., yourserver.database.windows.net)"
                    )
                    suggestions.append(
                        "2. Check that your IP address is allowed in Azure SQL firewall rules"
                    )
                    suggestions.append(
                        "3. Verify username and password are correct"
                    )
                    suggestions.append(
                        "4. Ensure the database name is correct"
                    )
                    suggestions.append(
                        "5. For Azure SQL, you may need to add: Encrypt=yes;TrustServerCertificate=no"
                    )
                    suggestions.append(
                        "6. If the client SSL store is the issue, try TrustServerCertificate=yes."
                    )

                raise RuntimeError(
                    f"Failed to connect to Azure SQL: {e}\n\n"
                    f"Troubleshooting steps:\n" + "\n".join(suggestions) + "\n\n"
                    f"Connection string format for Azure SQL:\n"
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER=server.database.windows.net,1433;"
                    f"DATABASE=mydb;UID=username;PWD=password;Encrypt=yes;TrustServerCertificate=no"
                ) from e

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> pyodbc.Connection:
        if self._conn is None:
            raise RuntimeError("DB not connected. Call connect() first.")
        return self._conn

    # ---------- STOCKS (StockDB) ----------

    def upsert_stock_instruments(
        self, stocks: Iterable[StockInstrument]
        ) -> None:
        """
        Append-only insert for StockDB, similar to upsert_option_instruments:

        - Deduplicates the input list by instrument_token.
        - Checks which instrument_tokens already exist in dbo.StockDB.
        - Inserts only the new ones.
        - Does NOT update or delete any existing rows.
        """
        stocks_list = list(stocks)
        if not stocks_list:
            return

        cursor = self.conn.cursor()

        # 1) Deduplicate by instrument_token within this batch
        seen_tokens: set[int] = set()
        unique_stocks: list[StockInstrument] = []
        for s in stocks_list:
            if s.instrument_token not in seen_tokens:
                seen_tokens.add(s.instrument_token)
                unique_stocks.append(s)

        if not unique_stocks:
            cursor.close()
            return

        # 2) Find which instrument_tokens already exist in StockDB
        tokens = {s.instrument_token for s in unique_stocks}

        existing_tokens: set[int] = set()
        if tokens:
            # SQL Server has a limit of 2100 parameters per query
            # Process tokens in chunks to avoid exceeding the limit
            max_tokens_per_query = 2000  # Safe limit below 2100
            token_list = list(tokens)

            for chunk_start in range(0, len(token_list), max_tokens_per_query):
                chunk_end = min(chunk_start + max_tokens_per_query, len(token_list))
                token_chunk = token_list[chunk_start:chunk_end]
                placeholders = ",".join("?" for _ in token_chunk)

                cursor.execute(
                    f"""
                    SELECT instrument_token
                    FROM dbo.StockDB
                    WHERE instrument_token IN ({placeholders})
                    """,
                    token_chunk,
                )
                for row in cursor.fetchall():
                    existing_tokens.add(int(row.instrument_token))

        # 3) Filter to only brand-new instrument_tokens
        new_stocks = [s for s in unique_stocks if s.instrument_token not in existing_tokens]

        if new_stocks:
            cursor.fast_executemany = True
            rows = [
                (
                    s.exchange,
                    s.tradingsymbol,
                    s.name,
                    s.instrument_token,
                    s.segment,
                    s.tick_size,
                    s.lot_size,
                )
                for s in new_stocks
            ]

            cursor.executemany(
                """
                INSERT INTO dbo.StockDB (
                    exchange,
                    tradingsymbol,
                    name,
                    instrument_token,
                    segment,
                    tick_size,
                    lot_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.conn.commit()

        cursor.close()


    def rebuild_stock_db(self, stocks: Iterable[StockInstrument]) -> None:
        stocks = list(stocks)
        cursor = self.conn.cursor()

        cursor.execute("TRUNCATE TABLE dbo.StockDB;")

        if stocks:
            cursor.fast_executemany = True
            rows = [
                (
                    s.exchange,
                    s.tradingsymbol,
                    s.name,
                    s.instrument_token,
                    s.segment,
                    s.tick_size,
                    s.lot_size,
                )
                for s in stocks
            ]

            cursor.executemany(
                """
                INSERT INTO dbo.StockDB (
                    exchange,
                    tradingsymbol,
                    name,
                    instrument_token,
                    segment,
                    tick_size,
                    lot_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        # Commit is not needed with autocommit=True, but keeping it for safety
        # If autocommit is False, this will commit the transaction
        if not self._conn.autocommit:
            self.conn.commit()

    def search_stocks_by_name(
        self, query: str, limit: int | None = None, segment: str | None = None
    ) -> List[StockInstrument]:
        """
        Search stocks by name/tradingsymbol, optionally filtered by segment.

        Args:
            query: Search term to match against name or tradingsymbol
            limit: Maximum number of results to return
            segment: Optional segment filter. Can be "NSE", "BSE", or "INDICES"
                    For INDICES, matches segments ending with "INDICES"
        """
        cursor = self.conn.cursor()
        pattern = f"%{query}%"

        # Build WHERE clause based on segment filter
        name_condition = "(name IS NOT NULL AND LOWER(name) LIKE LOWER(?))"
        symbol_condition = "(tradingsymbol IS NOT NULL AND LOWER(tradingsymbol) LIKE LOWER(?))"
        where_conditions = [f"({name_condition} OR {symbol_condition})"]

        # Prepare parameters
        params = [pattern, pattern]

        if segment:
            segment_upper = segment.upper()
            if segment_upper == "INDICES":
                # For indices, match segments that end with "INDICES"
                where_conditions.append("segment LIKE '%INDICES'")
            elif segment_upper in ("NSE", "BSE"):
                # For NSE/BSE stocks, match exact segment
                where_conditions.append("segment = ?")
                params.append(segment_upper)
            # If segment is something else, ignore it

        where_clause = " AND ".join(where_conditions)

        # Build SQL query with optional TOP clause
        # SQL Server doesn't support parameterized TOP, so use string formatting for limit
        # But keep parameters for the search pattern to prevent SQL injection
        if limit is not None:
            sql = f"""
            SELECT TOP {limit}
                exchange,
                tradingsymbol,
                name,
                instrument_token,
                segment,
                tick_size,
                lot_size
            FROM dbo.StockDB
            WHERE {where_clause}
            ORDER BY tradingsymbol
            """
        else:
            sql = f"""
            SELECT
                exchange,
                tradingsymbol,
                name,
                instrument_token,
                segment,
                tick_size,
                lot_size
            FROM dbo.StockDB
            WHERE {where_clause}
            ORDER BY tradingsymbol
            """

        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        cursor.close()

        results: List[StockInstrument] = []
        for r in rows:
            results.append(
                StockInstrument(
                    exchange=r.exchange,
                    tradingsymbol=r.tradingsymbol,
                    name=r.name,
                    instrument_token=r.instrument_token,
                    segment=r.segment,
                    tick_size=float(r.tick_size) if r.tick_size is not None else None,
                    lot_size=int(r.lot_size) if r.lot_size is not None else None,
                )
            )
        return results

    def get_stock_count(self) -> int:
        """Get total count of stocks in StockDB table. Useful for debugging."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM dbo.StockDB")
        count = cursor.fetchone()[0]
        return int(count)

    def get_stock_instruments_by_symbols(
        self,
        symbols: Iterable[str],
        exchange: str = "NSE",
    ) -> dict[str, StockInstrument]:
        """Return StockDB rows keyed by tradingsymbol for the requested symbols."""
        requested = sorted({symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()})
        if not requested:
            return {}

        cursor = self.conn.cursor()
        results: dict[str, StockInstrument] = {}
        max_symbols_per_query = 2000

        for chunk_start in range(0, len(requested), max_symbols_per_query):
            chunk = requested[chunk_start:chunk_start + max_symbols_per_query]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"""
                SELECT exchange, tradingsymbol, name, instrument_token, segment, tick_size, lot_size
                FROM dbo.StockDB
                WHERE exchange = ?
                  AND UPPER(tradingsymbol) IN ({placeholders})
                """,
                [exchange, *chunk],
            )
            for row in cursor.fetchall():
                instrument = StockInstrument(
                    exchange=row.exchange,
                    tradingsymbol=row.tradingsymbol,
                    name=row.name,
                    instrument_token=int(row.instrument_token),
                    segment=row.segment,
                    tick_size=float(row.tick_size) if row.tick_size is not None else None,
                    lot_size=int(row.lot_size) if row.lot_size is not None else None,
                )
                results[instrument.tradingsymbol.upper()] = instrument

        cursor.close()
        return results

    # ---------- OPTION INSTRUMENTS ----------

    def upsert_option_instruments(
        self, options: Iterable[OptionInstrument]
    ) -> None:
        options = list(options)
        if not options:
            return

        cursor = self.conn.cursor()

        tokens = {o.instrument_token for o in options}

        existing_tokens: set[int] = set()
        if tokens:
            # SQL Server has a limit of 2100 parameters per query
            # Process tokens in chunks to avoid exceeding the limit
            max_tokens_per_query = 2000  # Safe limit below 2100
            token_list = list(tokens)

            for chunk_start in range(0, len(token_list), max_tokens_per_query):
                chunk_end = min(chunk_start + max_tokens_per_query, len(token_list))
                token_chunk = token_list[chunk_start:chunk_end]
                params = ",".join("?" for _ in token_chunk)

                cursor.execute(
                    f"""
                    SELECT instrument_token
                    FROM dbo.OptionInstrument
                    WHERE instrument_token IN ({params})
                    """,
                    token_chunk,
                )
                for row in cursor.fetchall():
                    existing_tokens.add(int(row.instrument_token))

        new_options = [o for o in options if o.instrument_token not in existing_tokens]
        if new_options:
            cursor.fast_executemany = True
            rows = [
                (
                    o.fetch_date,
                    o.instrument_token,
                    o.underlying,
                    o.exchange,
                    o.tradingsymbol,
                    o.name,
                    o.strike,
                    o.expiry,
                    o.instrument_type,
                    o.lot_size,
                    o.tick_size,
                    o.segment,
                )
                for o in new_options
            ]
            cursor.executemany(
                """
                INSERT INTO dbo.OptionInstrument (
                    fetch_date,
                    instrument_token,
                    underlying,
                    exchange,
                    tradingsymbol,
                    name,
                    strike,
                    expiry,
                    instrument_type,
                    lot_size,
                    tick_size,
                    segment
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.conn.commit()

    def get_option_instrument_ids_by_token(
        self, tokens: Iterable[int]
    ) -> Dict[int, int]:
        token_list = list(tokens)
        if not token_list:
            return {}

        # SQL Server has a limit of 2100 parameters per query
        # Process tokens in chunks to avoid exceeding the limit
        max_tokens_per_query = 2000  # Safe limit below 2100
        cursor = self.conn.cursor()
        mapping: Dict[int, int] = {}

        for chunk_start in range(0, len(token_list), max_tokens_per_query):
            chunk_end = min(chunk_start + max_tokens_per_query, len(token_list))
            token_chunk = token_list[chunk_start:chunk_end]
            params = ",".join("?" for _ in token_chunk)

            cursor.execute(
                f"""
                SELECT instrument_token, id
                FROM dbo.OptionInstrument
                WHERE instrument_token IN ({params})
                """,
                token_chunk,
            )

            for row in cursor.fetchall():
                mapping[int(row.instrument_token)] = int(row.id)

        cursor.close()
        return mapping

    def get_option_instrument_by_id(self, option_instrument_id: int) -> Dict[str, Any] | None:
        """
        Get option instrument details by database ID.

        Returns:
            Dictionary with option instrument details or None if not found.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                tradingsymbol,
                strike,
                expiry,
                instrument_type,
                underlying,
                exchange,
                name
            FROM dbo.OptionInstrument
            WHERE id = ?
            """,
            (option_instrument_id,),
        )

        row = cursor.fetchone()
        cursor.close()

        if not row:
            return None

        return {
            "id": row.id,
            "tradingsymbol": row.tradingsymbol,
            "strike": float(row.strike) if row.strike is not None else None,
            "expiry": row.expiry,
            "instrument_type": row.instrument_type,
            "underlying": row.underlying,
            "exchange": row.exchange,
            "name": row.name,
        }

    # ---------- OPTION DATA (snapshots) ----------

    def bulk_insert_option_data(
        self, data_rows: Iterable[OptionData], batch_size: int = 1000
    ) -> None:
        """
        Store OptionData rows into dbo.OptionSnapshot.

        Uses UPDATE-then-INSERT upsert keyed on (option_instrument_id, trade_date,
        snapshot_label) to match the unique constraint added in migration 002.

        trade_date is derived from snapshot_time. snapshot_label defaults to
        CLOSE_1515 and data_source to KITE_QUOTE_LIVE for this legacy write path.

        OptionSnapshotCalc is calculated separately by
        scripts/calculate_option_snapshot_calc.py.
        """
        data_list = list(data_rows)
        if not data_list:
            return

        cursor = self.conn.cursor()

        upsert_sql = """
            UPDATE dbo.OptionSnapshot
            SET snapshot_time   = ?,
                underlying_price = ?,
                last_price       = ?,
                bid_price        = ?,
                bid_qty          = ?,
                ask_price        = ?,
                ask_qty          = ?,
                volume           = ?,
                open_interest    = ?,
                data_source      = ?
            WHERE option_instrument_id = ?
              AND trade_date           = ?
              AND snapshot_label       = ?;

            IF @@ROWCOUNT = 0
            BEGIN
                INSERT INTO dbo.OptionSnapshot (
                    option_instrument_id, snapshot_time,
                    underlying_price, last_price,
                    bid_price, bid_qty, ask_price, ask_qty,
                    volume, open_interest,
                    trade_date, snapshot_label, data_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            END
        """

        try:
            for batch_start in range(0, len(data_list), batch_size):
                batch = data_list[batch_start:batch_start + batch_size]
                for d in batch:
                    trade_date = d.snapshot_time.date()
                    snapshot_label = "CLOSE_1515"
                    data_source = "KITE_QUOTE_LIVE"
                    update_params = [
                        d.snapshot_time, d.underlying_price, d.last_price,
                        d.bid_price, d.bid_qty, d.ask_price, d.ask_qty,
                        d.volume, d.open_interest, data_source,
                        d.option_instrument_id, trade_date, snapshot_label,
                    ]
                    insert_params = [
                        d.option_instrument_id, d.snapshot_time,
                        d.underlying_price, d.last_price,
                        d.bid_price, d.bid_qty, d.ask_price, d.ask_qty,
                        d.volume, d.open_interest,
                        trade_date, snapshot_label, data_source,
                    ]
                    cursor.execute(upsert_sql, update_params + insert_params)
                self.conn.commit()

        except Exception as e:
            logger.error(f"bulk_insert_option_data failed: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def fetch_option_data(
        self,
        option_instrument_ids: Iterable[int],
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        ) -> List[OptionData]:
        """
        Read joined option data (raw + calculated) for a list of
        option_instrument_ids, optionally filtered by time window.

        Returns a list of OptionData objects.
        """
        ids = [int(x) for x in option_instrument_ids]
        if not ids:
            return []

        cursor = self.conn.cursor()

        # SQL Server has a limit of 2100 parameters per query
        # Process IDs in chunks to avoid exceeding the limit
        max_ids_per_query = 2000  # Safe limit below 2100 (accounting for time params)
        all_rows = []

        for chunk_start in range(0, len(ids), max_ids_per_query):
            chunk_end = min(chunk_start + max_ids_per_query, len(ids))
            id_chunk = ids[chunk_start:chunk_end]
            placeholders = ",".join("?" for _ in id_chunk)

            sql = f"""
                SELECT
                    s.option_instrument_id,
                    s.snapshot_time,
                    s.underlying_price,
                    s.last_price,
                    s.bid_price,
                    s.bid_qty,
                    s.ask_price,
                    s.ask_qty,
                    s.volume,
                    s.open_interest,
                    c.implied_volatility,
                    c.delta,
                    c.gamma,
                    c.theta,
                    c.vega
                FROM dbo.OptionSnapshot AS s
                LEFT JOIN dbo.OptionSnapshotCalc AS c
                    ON c.option_snapshot_id = s.id
                WHERE s.option_instrument_id IN ({placeholders})
            """

            params: list[object] = id_chunk

            if from_time is not None:
                sql += " AND s.snapshot_time >= ?"
                params.append(from_time)

            if to_time is not None:
                sql += " AND s.snapshot_time <= ?"
                params.append(to_time)

            sql += " ORDER BY s.option_instrument_id, s.snapshot_time"

            cursor.execute(sql, params)
            chunk_rows = cursor.fetchall()
            all_rows.extend(chunk_rows)

        cursor.close()
        rows = all_rows

        results: List[OptionData] = []
        for r in rows:
            # Convert snapshot_time to datetime if it's a string
            snapshot_time = r[1]
            if isinstance(snapshot_time, str):
                try:
                    snapshot_time = datetime.fromisoformat(snapshot_time.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    try:
                        snapshot_time = datetime.strptime(snapshot_time, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        try:
                            snapshot_time = datetime.strptime(snapshot_time, "%Y-%m-%d %H:%M:%S.%f")
                        except ValueError:
                            # Skip this row if we can't parse the date
                            continue
            elif not isinstance(snapshot_time, datetime):
                # If it's not datetime or string, try to convert
                continue

            results.append(
                OptionData(
                    option_instrument_id=r[0],
                    snapshot_time=snapshot_time,
                    underlying_price=float(r[2]) if r[2] is not None else None,
                    last_price=float(r[3]) if r[3] is not None else None,
                    bid_price=float(r[4]) if r[4] is not None else None,
                    bid_qty=int(r[5]) if r[5] is not None else None,
                    ask_price=float(r[6]) if r[6] is not None else None,
                    ask_qty=int(r[7]) if r[7] is not None else None,
                    volume=int(r[8]) if r[8] is not None else None,
                    open_interest=int(r[9]) if r[9] is not None else None,
                    implied_volatility=float(r[10]) if r[10] is not None else None,
                    delta=float(r[11]) if r[11] is not None else None,
                    gamma=float(r[12]) if r[12] is not None else None,
                    theta=float(r[13]) if r[13] is not None else None,
                    vega=float(r[14]) if r[14] is not None else None,
                )
            )

        return results

    def fetch_latest_option_chain_for_underlying(self, underlying: str) -> List[Dict[str, Any]]:
        """
        Get the latest snapshot (prices + IV + greeks) for all options
        of a given underlying (e.g. 'NIFTY', 'RELIANCE').
        Returns list of dicts ready to JSON-ify.
        """
        underlying = underlying.upper()

        # Try with snapshot_id first, fallback to direct join if view doesn't have it
        sql = """
        SELECT
            oi.id                AS option_instrument_id,
            oi.underlying,
            oi.tradingsymbol,
            oi.strike,
            oi.expiry,
            oi.instrument_type,
            v.snapshot_time,
            v.underlying_price,
            v.last_price,
            v.bid_price,
            v.bid_qty,
            v.ask_price,
            v.ask_qty,
            v.volume,
            v.open_interest,
            c.implied_volatility,
            c.delta,
            c.gamma,
            c.theta,
            c.vega
        FROM dbo.OptionInstrument AS oi
        INNER JOIN dbo.vw_OptionLatestSnapshot AS v
            ON v.option_instrument_id = oi.id
        LEFT JOIN dbo.OptionSnapshotCalc AS c
            ON c.option_snapshot_id = v.snapshot_id
        WHERE oi.underlying = ?
        ORDER BY oi.expiry, oi.strike, oi.instrument_type;
        """

        # Alternative query if view doesn't have snapshot_id - join directly via OptionSnapshot
        sql_alt = """
        SELECT
            oi.id                AS option_instrument_id,
            oi.underlying,
            oi.tradingsymbol,
            oi.strike,
            oi.expiry,
            oi.instrument_type,
            v.snapshot_time,
            v.underlying_price,
            v.last_price,
            v.bid_price,
            v.bid_qty,
            v.ask_price,
            v.ask_qty,
            v.volume,
            v.open_interest,
            c.implied_volatility,
            c.delta,
            c.gamma,
            c.theta,
            c.vega
        FROM dbo.OptionInstrument AS oi
        INNER JOIN (
            SELECT
                option_instrument_id,
                MAX(snapshot_time) AS max_time
            FROM dbo.OptionSnapshot
            GROUP BY option_instrument_id
        ) AS latest ON latest.option_instrument_id = oi.id
        INNER JOIN dbo.OptionSnapshot AS v
            ON v.option_instrument_id = oi.id
            AND v.snapshot_time = latest.max_time
        LEFT JOIN dbo.OptionSnapshotCalc AS c
            ON c.option_snapshot_id = v.id
        WHERE oi.underlying = ?
        ORDER BY oi.expiry, oi.strike, oi.instrument_type;
        """

        cur = self.conn.cursor()

        # Try the primary query first, fallback to alternative if it fails
        try:
            cur.execute(sql, (underlying,))
        except Exception as e:
            # If view doesn't have snapshot_id, use alternative query
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Primary query failed, trying alternative: {e}")
            sql = sql_alt
            cur.execute(sql, (underlying,))

        rows = cur.fetchall()

        cols = [d[0] for d in cur.description]
        result: List[Dict[str, Any]] = []
        for r in rows:
            row_dict: Dict[str, Any] = {}
            for i, col in enumerate(cols):
                value = r[i]
                # Handle datetime serialization
                if isinstance(value, datetime):
                    row_dict[col] = value.isoformat()
                elif isinstance(value, date):
                    row_dict[col] = value.isoformat()
                # Handle decimal/float conversion
                elif value is not None and hasattr(value, '__float__'):
                    try:
                        row_dict[col] = float(value)
                    except (ValueError, TypeError):
                        row_dict[col] = value
                # Handle int conversion
                elif value is not None and (isinstance(value, int) or hasattr(value, '__int__')):
                    try:
                        row_dict[col] = int(value)
                    except (ValueError, TypeError):
                        row_dict[col] = value
                else:
                    # Include NULL values explicitly
                    row_dict[col] = value
            result.append(row_dict)

        cur.close()
        return result

    # ---------- KITE ACCESS TOKEN ----------

    def save_kite_access_token(self, access_token: str) -> None:
        """
        Save or update the Kite access token in the database.
        Creates the table if it doesn't exist, then upserts the token.

        Args:
            access_token: The Kite Connect access token to save
        """
        cursor = self.conn.cursor()

        try:
            # Create table if it doesn't exist
            cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'dbo.KiteAccessToken') AND type in (N'U'))
                BEGIN
                    CREATE TABLE dbo.KiteAccessToken (
                        id INT PRIMARY KEY IDENTITY(1,1),
                        access_token NVARCHAR(MAX) NOT NULL,
                        created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                        updated_at DATETIME2 NOT NULL DEFAULT GETDATE()
                    )
                END
            """)

            # Add updated_at column if it doesn't exist (for existing tables)
            cursor.execute("""
                IF NOT EXISTS (
                    SELECT * FROM sys.columns
                    WHERE object_id = OBJECT_ID(N'dbo.KiteAccessToken')
                    AND name = 'updated_at'
                )
                BEGIN
                    ALTER TABLE dbo.KiteAccessToken
                    ADD updated_at DATETIME2 NOT NULL DEFAULT GETDATE()
                END
            """)

            # Check if a token already exists
            cursor.execute("SELECT COUNT(*) FROM dbo.KiteAccessToken")
            count = cursor.fetchone()[0]

            if count == 0:
                # Insert new token
                cursor.execute("""
                    INSERT INTO dbo.KiteAccessToken (access_token, created_at, updated_at)
                    VALUES (?, GETDATE(), GETDATE())
                """, (access_token,))
            else:
                # Update the most recently updated token (or first one if updated_at is NULL)
                # This ensures we always overwrite the active token
                cursor.execute("""
                    UPDATE dbo.KiteAccessToken
                    SET access_token = ?, updated_at = GETDATE()
                    WHERE id = (
                        SELECT TOP 1 id
                        FROM dbo.KiteAccessToken
                        ORDER BY updated_at DESC, id
                    )
                """, (access_token,))

            self.conn.commit()

        except Exception as e:
            self.conn.rollback()
            raise RuntimeError(f"Failed to save access token to database: {e}") from e
        finally:
            cursor.close()

    def get_kite_access_token(self) -> str | None:
        """
        Retrieve the latest Kite access token from the database.
        Tries both table name variations: kiteAccessToken and KiteAccessToken

        Returns:
            The access token string, or None if not found
        """
        cursor = self.conn.cursor()

        # Try both table name variations (case-insensitive, but explicit is better)
        table_names = ["dbo.kiteAccessToken", "dbo.KiteAccessToken", "kiteAccessToken", "KiteAccessToken"]

        for table_name in table_names:
            try:
                cursor.execute(f"""
                SELECT TOP 1 access_token
                    FROM {table_name}
                ORDER BY updated_at DESC
            """)

                row = cursor.fetchone()
                if row:
                    token = row[0]
                    # Ensure token is a string and clean it
                    if token:
                        return str(token).strip()
                # If we got here, table exists but no rows
                break
            except Exception as e:
                # Table doesn't exist with this name, try next variation
                continue

        return None

    # ---------- WATCHED INSTRUMENTS ----------

    def upsert_watched_instruments(self, rows: Iterable[WatchedInstrument]) -> int:
        instruments = list(rows)
        if not instruments:
            return 0

        cursor = self.conn.cursor()
        cursor.fast_executemany = True
        cursor.executemany(
            """
            MERGE INTO dbo.WatchedInstrument AS T
            USING (SELECT ? AS tradingsymbol, ? AS exchange) AS S
                ON T.tradingsymbol = S.tradingsymbol AND T.exchange = S.exchange
            WHEN MATCHED THEN
                UPDATE SET
                    name             = ?,
                    instrument_token = ?,
                    segment          = ?,
                    tick_size        = ?,
                    lot_size         = ?,
                    instrument_type  = ?,
                    sector           = ?,
                    industry         = ?,
                    is_fo_enabled    = ?,
                    is_active        = ?,
                    updated_at       = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (tradingsymbol, exchange, name, instrument_token, segment,
                        tick_size, lot_size, instrument_type, sector, industry,
                        is_fo_enabled, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            [
                (
                    w.tradingsymbol,
                    w.exchange,
                    w.name,
                    w.instrument_token,
                    w.segment,
                    w.tick_size,
                    w.lot_size,
                    w.instrument_type,
                    w.sector,
                    w.industry,
                    int(w.is_fo_enabled),
                    int(w.is_active),
                    w.tradingsymbol,
                    w.exchange,
                    w.name,
                    w.instrument_token,
                    w.segment,
                    w.tick_size,
                    w.lot_size,
                    w.instrument_type,
                    w.sector,
                    w.industry,
                    int(w.is_fo_enabled),
                    int(w.is_active),
                )
                for w in instruments
            ],
        )
        cursor.close()
        logger.info("Upserted %d WatchedInstrument rows.", len(instruments))
        return len(instruments)

    def seed_watched_from_stockdb(self) -> int:
        """Clone NIFTY/BANKNIFTY rows from StockDB into WatchedInstrument."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.WatchedInstrument
                (tradingsymbol, exchange, name, instrument_token, segment,
                 tick_size, lot_size, instrument_type, is_fo_enabled, is_active)
            SELECT
                CASE s.tradingsymbol
                    WHEN 'NIFTY 50'   THEN 'NIFTY'
                    WHEN 'NIFTY BANK' THEN 'BANKNIFTY'
                END,
                s.exchange,
                s.name,
                s.instrument_token,
                s.segment,
                s.tick_size,
                s.lot_size,
                'INDEX',
                1,
                1
            FROM dbo.StockDB s
            WHERE s.tradingsymbol IN ('NIFTY 50', 'NIFTY BANK')
              AND s.segment LIKE '%INDICES%'
              AND NOT EXISTS (
                  SELECT 1 FROM dbo.WatchedInstrument w
                  WHERE w.exchange = s.exchange
                    AND w.tradingsymbol = CASE s.tradingsymbol
                        WHEN 'NIFTY 50'   THEN 'NIFTY'
                        WHEN 'NIFTY BANK' THEN 'BANKNIFTY'
                    END
              );
            """
        )
        count = cursor.rowcount
        cursor.close()
        logger.info("Seeded %d WatchedInstrument rows from StockDB.", count)
        return count

    def get_active_underlyings(self, instrument_type: str | None = None) -> list[str]:
        """Return tradingsymbols of all active watched instruments."""
        cursor = self.conn.cursor()
        if instrument_type:
            cursor.execute(
                """
                SELECT tradingsymbol
                FROM dbo.WatchedInstrument
                WHERE is_active = 1 AND instrument_type = ?
                ORDER BY tradingsymbol
                """,
                (instrument_type,),
            )
        else:
            cursor.execute(
                """
                SELECT tradingsymbol
                FROM dbo.WatchedInstrument
                WHERE is_active = 1
                ORDER BY tradingsymbol
                """
            )
        rows = cursor.fetchall()
        cursor.close()
        return [r[0] for r in rows]

    def get_watched_symbol_set(
        self,
        exchange: str | None = None,
        instrument_type: str | None = None,
    ) -> set[str]:
        """Return active watched symbols, optionally filtered by exchange/type."""
        cursor = self.conn.cursor()
        sql = """
            SELECT tradingsymbol
            FROM dbo.WatchedInstrument
            WHERE is_active = 1
        """
        params: list[object] = []
        if exchange:
            sql += " AND exchange = ?"
            params.append(exchange)
        if instrument_type:
            sql += " AND instrument_type = ?"
            params.append(instrument_type)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
        return {str(row[0]).upper() for row in rows}

    def get_watched_instruments(
        self,
        instrument_type: str | None = None,
    ) -> list[WatchedInstrument]:
        """Return full WatchedInstrument rows for active instruments."""
        cursor = self.conn.cursor()
        sql = """
            SELECT watched_id, tradingsymbol, exchange, name, instrument_token,
                   segment, tick_size, lot_size, instrument_type, sector, industry,
                   is_fo_enabled, is_active
            FROM dbo.WatchedInstrument
            WHERE is_active = 1
        """
        params: list[object] = []
        if instrument_type:
            sql += " AND instrument_type = ?"
            params.append(instrument_type)
        sql += " ORDER BY tradingsymbol"

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()

        return [
            WatchedInstrument(
                watched_id=r.watched_id,
                tradingsymbol=r.tradingsymbol,
                exchange=r.exchange,
                name=r.name,
                instrument_token=int(r.instrument_token) if r.instrument_token else None,
                segment=r.segment,
                tick_size=float(r.tick_size) if r.tick_size is not None else None,
                lot_size=int(r.lot_size) if r.lot_size is not None else None,
                instrument_type=r.instrument_type,
                sector=r.sector,
                industry=r.industry,
                is_fo_enabled=bool(r.is_fo_enabled),
                is_active=bool(r.is_active),
            )
            for r in rows
        ]

    # ---------- TRADING CALENDAR ----------

    def upsert_trading_calendar(self, rows: list[dict]) -> int:
        """
        Upsert rows into dbo.TradingCalendar.
        Each dict must have: calendar_date, exchange, is_trading_day,
        is_weekly_expiry, is_monthly_expiry, is_special_session, notes (optional).
        """
        if not rows:
            return 0
        cursor = self.conn.cursor()
        cursor.fast_executemany = True
        cursor.executemany(
            """
            MERGE INTO dbo.TradingCalendar AS T
            USING (SELECT ? AS calendar_date, ? AS exchange) AS S
                ON T.calendar_date = S.calendar_date AND T.exchange = S.exchange
            WHEN MATCHED THEN
                UPDATE SET
                    is_trading_day     = ?,
                    is_weekly_expiry   = ?,
                    is_monthly_expiry  = ?,
                    is_special_session = ?,
                    notes              = ?,
                    updated_at         = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (calendar_date, exchange, is_trading_day, is_weekly_expiry,
                        is_monthly_expiry, is_special_session, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            [
                (
                    r["calendar_date"], r["exchange"],
                    int(r["is_trading_day"]), int(r["is_weekly_expiry"]),
                    int(r["is_monthly_expiry"]), int(r.get("is_special_session", 0)),
                    r.get("notes"),
                    r["calendar_date"], r["exchange"],
                    int(r["is_trading_day"]), int(r["is_weekly_expiry"]),
                    int(r["is_monthly_expiry"]), int(r.get("is_special_session", 0)),
                    r.get("notes"),
                )
                for r in rows
            ],
        )
        cursor.close()
        return len(rows)

    def get_next_trading_day(self, signal_date: date, exchange: str = "NSE") -> Optional[date]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT TOP 1 calendar_date
            FROM dbo.TradingCalendar
            WHERE exchange = ? AND calendar_date > ? AND is_trading_day = 1
            ORDER BY calendar_date
            """,
            (exchange, signal_date),
        )
        row = cursor.fetchone()
        cursor.close()
        if row:
            return row[0].date() if isinstance(row[0], datetime) else row[0]
        return None

    def is_trading_day(self, check_date: date, exchange: str = "NSE") -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT is_trading_day FROM dbo.TradingCalendar WHERE calendar_date = ? AND exchange = ?",
            (check_date, exchange),
        )
        row = cursor.fetchone()
        cursor.close()
        return bool(row[0]) if row else False

    def is_expiry_day(self, check_date: date, exchange: str = "NSE") -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT CASE WHEN is_weekly_expiry = 1 OR is_monthly_expiry = 1 THEN 1 ELSE 0 END
            FROM dbo.TradingCalendar
            WHERE calendar_date = ? AND exchange = ?
            """,
            (check_date, exchange),
        )
        row = cursor.fetchone()
        cursor.close()
        return bool(row[0]) if row else False

    # ---------- SIGNAL FEATURES ----------

    def upsert_signal_features(self, rows: list[dict]) -> int:
        """Upsert rows into dbo.SignalFeatureDaily. Each dict maps column name -> value."""
        if not rows:
            return 0
        cols = [
            "signal_date", "symbol", "instrument_type",
            "open_915", "close_1515", "high_day", "low_day", "volume_day",
            "ret_1d", "ret_3d", "ret_5d", "ret_10d", "ret_20d",
            "sma_5", "sma_10", "sma_20", "ema_5", "ema_10", "ema_20",
            "close_vs_sma_5", "close_vs_sma_10", "close_vs_sma_20",
            "rsi_14", "macd", "macd_signal", "macd_hist", "roc_5", "roc_10",
            "atr_14", "atr_pct", "realized_vol_5d", "realized_vol_10d", "realized_vol_20d",
            "day_range_pct", "gap_pct", "close_position_in_range",
            "futures_oi_change_pct", "futures_volume_change_pct",
            "pcr_oi", "pcr_volume", "atm_iv", "iv_rank_20d", "skew_put_call",
            "max_oi_call_strike", "max_oi_put_strike",
            "distance_from_max_call_oi_pct", "distance_from_max_put_oi_pct",
            "macro_score", "news_score", "event_risk_score",
            "feature_version", "source_quality_score",
            "reason_json", "strategy_features_json",
        ]
        update_cols = [c for c in cols if c not in ("signal_date", "symbol", "feature_version")]
        set_clause = ",\n                    ".join(f"{c} = ?" for c in update_cols)
        insert_cols = ", ".join(cols)
        insert_vals = ", ".join("?" for _ in cols)

        cursor = self.conn.cursor()
        cursor.fast_executemany = True

        def _row_vals(r: dict) -> tuple:
            return tuple(r.get(c) for c in cols)

        def _update_vals(r: dict) -> tuple:
            return tuple(r.get(c) for c in update_cols)

        cursor.executemany(
            f"""
            MERGE INTO dbo.SignalFeatureDaily AS T
            USING (SELECT ? AS signal_date, ? AS symbol, ? AS feature_version) AS S
                ON T.signal_date = S.signal_date AND T.symbol = S.symbol
                   AND T.feature_version = S.feature_version
            WHEN MATCHED THEN
                UPDATE SET {set_clause}, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT ({insert_cols}) VALUES ({insert_vals});
            """,
            [
                (r.get("signal_date"), r.get("symbol"), r.get("feature_version", "v1"))
                + _update_vals(r)
                + _row_vals(r)
                for r in rows
            ],
        )
        cursor.close()
        return len(rows)

    # ---------- SIGNAL PREDICTIONS ----------

    def upsert_signal_predictions(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cursor = self.conn.cursor()
        cursor.fast_executemany = True
        cursor.executemany(
            """
            MERGE INTO dbo.SignalPrediction AS T
            USING (SELECT ? AS signal_date, ? AS trade_date, ? AS symbol,
                          ? AS model_name, ? AS model_version) AS S
                ON T.signal_date   = S.signal_date
               AND T.trade_date    = S.trade_date
               AND T.symbol        = S.symbol
               AND T.model_name    = S.model_name
               AND T.model_version = S.model_version
            WHEN MATCHED THEN
                UPDATE SET
                    instrument_type  = ?,
                    direction        = ?,
                    confidence       = ?,
                    expected_move_pct = ?,
                    trade_allowed    = ?,
                    no_trade_reason  = ?,
                    feature_id       = ?,
                    reason_json      = ?
            WHEN NOT MATCHED THEN
                INSERT (signal_date, trade_date, symbol, instrument_type,
                        model_name, model_version, direction, confidence,
                        expected_move_pct, trade_allowed, no_trade_reason,
                        feature_id, reason_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            [
                (
                    r["signal_date"], r["trade_date"], r["symbol"],
                    r["model_name"], r["model_version"],
                    r.get("instrument_type"), r["direction"], r["confidence"],
                    r.get("expected_move_pct"), int(r.get("trade_allowed", 0)),
                    r.get("no_trade_reason"), r.get("feature_id"), r.get("reason_json"),
                    r["signal_date"], r["trade_date"], r["symbol"],
                    r.get("instrument_type"), r["model_name"], r["model_version"],
                    r["direction"], r["confidence"], r.get("expected_move_pct"),
                    int(r.get("trade_allowed", 0)), r.get("no_trade_reason"),
                    r.get("feature_id"), r.get("reason_json"),
                )
                for r in rows
            ],
        )
        cursor.close()
        return len(rows)

    # ---------- SIGNAL LABELS ----------

    def upsert_signal_labels(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cursor = self.conn.cursor()
        cursor.fast_executemany = True
        cursor.executemany(
            """
            MERGE INTO dbo.SignalBacktestLabel AS T
            USING (SELECT ? AS signal_date, ? AS trade_date,
                          ? AS symbol, ? AS label_version) AS S
                ON T.signal_date   = S.signal_date
               AND T.trade_date    = S.trade_date
               AND T.symbol        = S.symbol
               AND T.label_version = S.label_version
            WHEN MATCHED THEN
                UPDATE SET
                    entry_time             = ?,
                    exit_time              = ?,
                    entry_price            = ?,
                    exit_price             = ?,
                    realized_return_pct    = ?,
                    positive_threshold_pct = ?,
                    negative_threshold_pct = ?,
                    actual_label           = ?,
                    updated_at             = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (signal_date, trade_date, symbol, entry_time, exit_time,
                        entry_price, exit_price, realized_return_pct,
                        positive_threshold_pct, negative_threshold_pct,
                        actual_label, label_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            [
                (
                    r["signal_date"], r["trade_date"], r["symbol"],
                    r.get("label_version", "v1"),
                    r.get("entry_time"), r.get("exit_time"),
                    r.get("entry_price"), r.get("exit_price"),
                    r.get("realized_return_pct"),
                    r.get("positive_threshold_pct"), r.get("negative_threshold_pct"),
                    r.get("actual_label"),
                    r["signal_date"], r["trade_date"], r["symbol"],
                    r.get("entry_time"), r.get("exit_time"),
                    r.get("entry_price"), r.get("exit_price"),
                    r.get("realized_return_pct"),
                    r.get("positive_threshold_pct"), r.get("negative_threshold_pct"),
                    r.get("actual_label"), r.get("label_version", "v1"),
                )
                for r in rows
            ],
        )
        cursor.close()
        return len(rows)
