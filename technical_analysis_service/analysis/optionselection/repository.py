from __future__ import annotations


from datetime import date
from typing import Any

from .schema import OptionContract, OptionType


def load_option_chain_with_calcs(
    db_client,
    underlying: str,
    as_of_time: str | None = None,
    min_expiry_date: str | None = None,
    max_expiry_date: str | None = None,
) -> list[OptionContract]:
    """
    Return latest option chain rows with IV and Greeks loaded from OptionSnapshotCalc.

    This function reads stored IV/Greeks. It intentionally does not calculate
    Black-Scholes values.
    """
    conn = getattr(db_client, "conn", db_client)
    is_postgres = getattr(db_client, "db_kind", "") == "postgres"
    cursor = conn.cursor()
    params: list[Any] = [underlying.upper()]
    as_of_filter = ""
    if as_of_time is not None:
        as_of_filter = "AND os.snapshot_time <= %s" if is_postgres else "AND os.snapshot_time <= ?"
        params.append(as_of_time)

    expiry_floor = min_expiry_date or (str(as_of_time)[:10] if as_of_time is not None else date.today().isoformat())
    placeholder = "%s" if is_postgres else "?"
    expiry_filters = [f"oi.expiry >= {placeholder}"]
    params.append(expiry_floor)
    if max_expiry_date is not None:
        expiry_filters.append(f"oi.expiry <= {placeholder}")
        params.append(max_expiry_date)

    expiry_sql = " AND ".join(expiry_filters)
    option_snapshot = '"OptionSnapshot"' if is_postgres else "dbo.OptionSnapshot"
    option_instrument = '"OptionInstrument"' if is_postgres else "dbo.OptionInstrument"
    option_calc = '"OptionSnapshotCalc"' if is_postgres else "dbo.OptionSnapshotCalc"
    sql = f"""
    WITH latest AS (
        SELECT
            os.id AS snapshot_id,
            os.option_instrument_id,
            os.snapshot_time,
            ROW_NUMBER() OVER (
                PARTITION BY os.option_instrument_id
                ORDER BY os.snapshot_time DESC, os.id DESC
            ) AS rn
        FROM {option_snapshot} os
        INNER JOIN {option_instrument} oi
            ON oi.id = os.option_instrument_id
        WHERE UPPER(oi.underlying) = {placeholder}
          {as_of_filter}
          AND {expiry_sql}
          AND os.last_price IS NOT NULL
          AND os.last_price > 0
    )
    SELECT
        oi.instrument_token,
        oi.tradingsymbol,
        oi.underlying,
        oi.expiry,
        oi.strike,
        oi.instrument_type,
        os.last_price,
        os.bid_price,
        os.ask_price,
        os.volume,
        os.open_interest,
        os.snapshot_time,
        calc.created_at AS calc_time,
        calc.implied_volatility,
        calc.delta,
        calc.gamma,
        calc.theta,
        calc.vega
    FROM latest l
    INNER JOIN {option_snapshot} os
        ON os.id = l.snapshot_id
    INNER JOIN {option_instrument} oi
        ON oi.id = os.option_instrument_id
    LEFT JOIN {option_calc} calc
        ON calc.option_snapshot_id = os.id
    WHERE l.rn = 1
    ORDER BY oi.expiry, oi.strike, oi.instrument_type;
    """

    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    return [_row_to_contract(row) for row in rows]


def _row_to_contract(row: Any) -> OptionContract:
    instrument_token = _get(row, "instrument_token", 0)
    tradingsymbol = _get(row, "tradingsymbol", 1)
    underlying = _get(row, "underlying", 2)
    expiry = _get(row, "expiry", 3)
    strike = _get(row, "strike", 4)
    instrument_type = _get(row, "instrument_type", 5)
    option_type = _option_type(instrument_type, tradingsymbol)
    return OptionContract(
        instrument_token=_int_or_none(instrument_token),
        tradingsymbol=str(tradingsymbol),
        underlying=str(underlying).upper(),
        expiry=_date_str(expiry),
        strike=float(strike),
        option_type=option_type,
        last_price=float(_get(row, "last_price", 6)),
        bid=_float_or_none(_get(row, "bid_price", 7)),
        ask=_float_or_none(_get(row, "ask_price", 8)),
        volume=_int_or_none(_get(row, "volume", 9)),
        open_interest=_int_or_none(_get(row, "open_interest", 10)),
        snapshot_time=_date_str(_get(row, "snapshot_time", 11)) if _get(row, "snapshot_time", 11) is not None else None,
        calc_time=_date_str(_get(row, "calc_time", 12)) if _get(row, "calc_time", 12) is not None else None,
        iv=_float_or_none(_get(row, "implied_volatility", 13)),
        delta=_float_or_none(_get(row, "delta", 14)),
        gamma=_float_or_none(_get(row, "gamma", 15)),
        theta=_float_or_none(_get(row, "theta", 16)),
        vega=_float_or_none(_get(row, "vega", 17)),
    )


def _get(row: Any, name: str, index: int) -> Any:
    if hasattr(row, name):
        return getattr(row, name)
    return row[index]


def _option_type(instrument_type: object, tradingsymbol: object) -> OptionType:
    raw = str(instrument_type or "").upper()
    sym = str(tradingsymbol or "").upper()
    if raw in {"CE", "PE"}:
        return raw  # type: ignore[return-value]
    if sym.endswith("PE"):
        return "PE"
    return "CE"


def _float_or_none(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_str(value: object) -> str:
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        if isinstance(value, date):
            return text
        return str(text)
    return str(value)
