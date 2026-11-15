# options_data.py
"""
Fetch option + underlying (spot) + futures-activity-proxy data WITHOUT relying on any view.

Reads from:
- dbo.OptionInstrument
- dbo.OptionSnapshot              (2 rows/day: 09:15 = "open", 15:15 = "close")
- dbo.OptionSnapshotCalc
- dbo.UnderlyingSnapshot          (spot/index daily OHLC; 1 row/day via trade_date)
- dbo.MarketActivityDaily         (near-month index futures daily OHLC + OI/volume proxies; 1 row/day)

Key behavior (as requested):
- Underlying "price" is derived from UnderlyingSnapshot directly:
    - if snapshot_time is 09:15 -> use UnderlyingSnapshot.open_price
    - if snapshot_time is 15:15 -> use UnderlyingSnapshot.close_price
- Futures OI/volume proxies come from MarketActivityDaily.
- Option data continues from OptionInstrument/OptionSnapshot/OptionSnapshotCalc.

Returned columns match your earlier expectations as closely as possible:
- EOD fetch reduces to LAST snapshot_time per (trade_date, instrument_token) (typically 15:15).

Note:
- UnderlyingSnapshot.volume for NIFTY/BANKNIFTY may be 0/NULL; you should use fut_* proxies instead.
"""

from __future__ import annotations


from typing import Iterable, Optional, List
import pandas as pd
import pyodbc

from technical_analysis_service.common.config import get_settings
from technical_analysis_service.data_manager.db.client_factory import get_database_client


# -----------------------------
# Internal helpers
# -----------------------------
def _classify_option_side(row) -> str:
    """
    Classify each option row as 'CALL', 'PUT', or 'UNKNOWN'.

    Priority:
      1) tradingsymbol ending with CE/PE
      2) sign of delta (>=0 => CALL, <0 => PUT)
    """
    sym = str(row.get("tradingsymbol") or "")

    if sym.endswith("CE"):
        return "CALL"
    if sym.endswith("PE"):
        return "PUT"

    delta = row.get("delta")
    try:
        if pd.notna(delta):
            return "CALL" if float(delta) >= 0 else "PUT"
    except Exception:
        pass

    return "UNKNOWN"


def _to_datestr(d) -> str:
    return pd.to_datetime(d).date().isoformat()


def get_active_underlyings(instrument_type: str | None = "INDEX") -> list[str]:
    """Return tradingsymbols of active watched instruments from WatchedInstrument."""
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        return db.get_active_underlyings(instrument_type=instrument_type)
    finally:
        db.close()


# -----------------------------
# Public API
# -----------------------------
def fetch_index_options_eod(
    conn: pyodbc.Connection,
    start_date=None,
    end_date=None,
    underlying_like: str = "NIFTY%",   # tolerant: NIFTY, NIFTY 50, etc.
    option_instrument_table: str = "dbo.OptionInstrument",
    option_snapshot_table: str = "dbo.OptionSnapshot",
    option_calc_table: str = "dbo.OptionSnapshotCalc",
    underlying_snapshot_table: str = "dbo.UnderlyingSnapshot",
    market_activity_table: str = "dbo.MarketActivityDaily",
) -> pd.DataFrame:
    """
    Fetch index option snapshots for the given date range, then
    reduce to one "EOD" snapshot per (trade_date, instrument_token)
    by taking the last snapshot_time of the day.

    Underlying spot price is sourced from UnderlyingSnapshot (NOT from OptionSnapshot):
      - 09:15 -> spot open_price
      - 15:15 -> spot close_price

    Futures OI/volume proxies come from MarketActivityDaily (near-month index futures rows).

    Returns a DataFrame with at least:
      instrument_token, tradingsymbol, strike, expiry, lot_size,
      underlying_price, option_price, option_volume, open_interest,
      implied_volatility, delta, gamma,
      trade_date, option_side,
      plus spot_ohlc_* and fut_* proxy columns.
    """

    sql = f"""
    SELECT
        oi.instrument_token,
        oi.underlying,
        os.snapshot_time,
        oi.tradingsymbol,
        oi.instrument_type,
        oi.strike,
        oi.expiry,
        oi.lot_size,

        -- Option snapshot values
        os.last_price           AS option_price,
        os.volume              AS option_volume,
        os.open_interest        AS open_interest,

        -- Option greeks/IV
        osc.implied_volatility  AS implied_volatility,
        osc.delta               AS delta,
        osc.gamma               AS gamma,

        -- Spot (index) daily OHLC from UnderlyingSnapshot (1 row per trade_date)
        us.open_price           AS spot_open,
        us.high_price           AS spot_high,
        us.low_price            AS spot_low,
        us.close_price          AS spot_close,
        us.volume               AS spot_volume,

        -- Underlying price at snapshot time derived from spot OHLC (because OptionSnapshot has 2 rows/day)
        CASE
            WHEN CONVERT(time, os.snapshot_time) = '09:15:00' THEN us.open_price
            WHEN CONVERT(time, os.snapshot_time) = '15:15:00' THEN us.close_price
            ELSE NULL
        END                     AS underlying_price,

        -- Futures proxy (near-month index futures) from MarketActivityDaily
        mad.expiry_date         AS fut_expiry_date,
        mad.fin_instrm_tp       AS fut_fin_instrm_tp,
        mad.tckr_symb           AS fut_tckr_symb,
        mad.fin_instrm_nm       AS fut_fin_instrm_nm,

        mad.open_price          AS fut_open_price,
        mad.high_price          AS fut_high_price,
        mad.low_price           AS fut_low_price,
        mad.close_price         AS fut_close_price,
        mad.settle_price        AS fut_settle_price,
        mad.underlying_price    AS fut_underlying_price,

        mad.open_interest       AS fut_open_interest,
        mad.change_in_oi        AS fut_change_in_oi,
        mad.traded_volume       AS fut_traded_volume,
        mad.traded_value        AS fut_traded_value

    FROM {option_snapshot_table} os
    INNER JOIN {option_instrument_table} oi
        ON oi.id = os.option_instrument_id
    LEFT JOIN {option_calc_table} osc
        ON osc.option_snapshot_id = os.id

    LEFT JOIN {underlying_snapshot_table} us
        ON us.underlying = oi.underlying
       AND us.trade_date = CAST(os.snapshot_time AS date)

    LEFT JOIN {market_activity_table} mad
        ON mad.underlying = oi.underlying
       AND mad.trade_date = CAST(os.snapshot_time AS date)
       AND mad.fin_instrm_tp = 'IDF'
       AND mad.tckr_symb = oi.underlying

    WHERE os.last_price IS NOT NULL
      AND oi.underlying LIKE ?
    """

    params: List = [underlying_like]

    if start_date is not None:
        sql += " AND CAST(os.snapshot_time AS date) >= ?"
        params.append(_to_datestr(start_date))

    if end_date is not None:
        sql += " AND CAST(os.snapshot_time AS date) <= ?"
        params.append(_to_datestr(end_date))

    sql += " ORDER BY os.snapshot_time, oi.strike;"

    df = pd.read_sql(sql, conn, params=params)
    if df.empty:
        return df

    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"])
    df["trade_date"] = df["snapshot_time"].dt.normalize()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
    if "fut_expiry_date" in df.columns:
        df["fut_expiry_date"] = pd.to_datetime(df["fut_expiry_date"], errors="coerce")

    # Take the LAST snapshot per (trade_date, instrument_token) as "EOD"
    df = (
        df.sort_values(["instrument_token", "snapshot_time"])
          .groupby(["trade_date", "instrument_token"], as_index=False)
          .tail(1)
    )

    # Classify CALL / PUT
    df["option_side"] = df.apply(_classify_option_side, axis=1)

    return df


def fetch_option_intraday_prices(
    conn: pyodbc.Connection,
    instrument_tokens: Iterable,
    start_date,
    end_date,
    option_instrument_table: str = "dbo.OptionInstrument",
    option_snapshot_table: str = "dbo.OptionSnapshot",
    underlying_snapshot_table: str = "dbo.UnderlyingSnapshot",
    market_activity_table: str = "dbo.MarketActivityDaily",
    include_proxies: bool = False,
) -> pd.DataFrame:
    """
    Fetch ALL option snapshots for the given instrument_tokens between start_date and end_date (inclusive).

    Keeps same behavior as your old version:
    - option_backtest.py can treat earliest snapshot in day as entry (09:15)
      and latest as exit (15:15)

    Underlying "price" for each snapshot is derived from UnderlyingSnapshot:
      - 09:15 -> spot open_price
      - 15:15 -> spot close_price

    Returns DataFrame with columns:
      instrument_token, snapshot_time, trade_date,
      option_price, underlying_price, lot_size

    If include_proxies=True, also includes futures proxy columns (fut_*).
    """

    # Deduplicate & clean tokens
    tokens = sorted({int(t) for t in instrument_tokens if pd.notna(t)})
    if not tokens:
        return pd.DataFrame()

    start_str = _to_datestr(start_date)
    end_str = _to_datestr(end_date)

    chunk_size = 100
    dfs = []

    for i in range(0, len(tokens), chunk_size):
        chunk_tokens = tokens[i:i + chunk_size]
        placeholders = ",".join("?" for _ in chunk_tokens)

        if include_proxies:
            sql = f"""
            SELECT
                oi.instrument_token,
                os.snapshot_time,
                os.last_price AS option_price,
                oi.lot_size,

                -- spot daily OHLC from UnderlyingSnapshot
                us.open_price AS spot_open,
                us.close_price AS spot_close,

                CASE
                    WHEN CONVERT(time, os.snapshot_time) = '09:15:00' THEN us.open_price
                    WHEN CONVERT(time, os.snapshot_time) = '15:15:00' THEN us.close_price
                    ELSE NULL
                END AS underlying_price,

                -- futures proxies (same trade_date)
                mad.open_interest AS fut_open_interest,
                mad.change_in_oi  AS fut_change_in_oi,
                mad.traded_volume AS fut_traded_volume,
                mad.traded_value  AS fut_traded_value,
                mad.close_price   AS fut_close_price,
                mad.settle_price  AS fut_settle_price
            FROM {option_snapshot_table} os
            INNER JOIN {option_instrument_table} oi
                ON oi.id = os.option_instrument_id
            LEFT JOIN {underlying_snapshot_table} us
                ON us.underlying = oi.underlying
               AND us.trade_date = CAST(os.snapshot_time AS date)
            LEFT JOIN {market_activity_table} mad
                ON mad.underlying = oi.underlying
               AND mad.trade_date = CAST(os.snapshot_time AS date)
               AND mad.fin_instrm_tp = 'IDF'
               AND mad.tckr_symb = oi.underlying
            WHERE oi.instrument_token IN ({placeholders})
              AND CAST(os.snapshot_time AS date) >= ?
              AND CAST(os.snapshot_time AS date) <= ?
            ORDER BY oi.instrument_token, os.snapshot_time;
            """
        else:
            sql = f"""
            SELECT
                oi.instrument_token,
                os.snapshot_time,
                os.last_price AS option_price,
                oi.lot_size,

                CASE
                    WHEN CONVERT(time, os.snapshot_time) = '09:15:00' THEN us.open_price
                    WHEN CONVERT(time, os.snapshot_time) = '15:15:00' THEN us.close_price
                    ELSE NULL
                END AS underlying_price
            FROM {option_snapshot_table} os
            INNER JOIN {option_instrument_table} oi
                ON oi.id = os.option_instrument_id
            LEFT JOIN {underlying_snapshot_table} us
                ON us.underlying = oi.underlying
               AND us.trade_date = CAST(os.snapshot_time AS date)
            WHERE oi.instrument_token IN ({placeholders})
              AND CAST(os.snapshot_time AS date) >= ?
              AND CAST(os.snapshot_time AS date) <= ?
            ORDER BY oi.instrument_token, os.snapshot_time;
            """

        params = list(chunk_tokens) + [start_str, end_str]
        chunk_df = pd.read_sql(sql, conn, params=params)
        if not chunk_df.empty:
            dfs.append(chunk_df)

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"])
    df["trade_date"] = df["snapshot_time"].dt.normalize()

    return df
