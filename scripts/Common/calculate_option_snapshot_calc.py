# scripts/calculate_option_snapshot_calc.py
"""
Calculate dbo.OptionSnapshotCalc rows from dbo.OptionSnapshot rows.

This script is intentionally separate from snapshot ingestion:
  - daily_NIFTYoption_snapshot.py writes live quote OptionSnapshot rows only.
  - this script computes valuation, IV, and Greeks for forward paper-trading snapshots.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.db.database_client import DatabaseClient

load_dotenv(project_root / ".env")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Settings:
    option_snapshot_table: str = "dbo.OptionSnapshot"
    option_instrument_table: str = "dbo.OptionInstrument"
    option_snapshot_calc_table: str = "dbo.OptionSnapshotCalc"
    risk_free_rate: float = 0.06
    dividend_yield: float = 0.00


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


def ensure_calc_index(db: DatabaseClient, settings: Settings) -> None:
    if getattr(db, "db_kind", "") == "postgres":
        if hasattr(db, "create_core_tables"):
            db.create_core_tables()
        return

    full_name = table_object_name(settings.option_snapshot_calc_table)
    safe_calc = safe_table_name(settings.option_snapshot_calc_table)
    cursor = db.conn.cursor()
    cursor.execute(
        f"""
        IF NOT EXISTS (
            SELECT 1
            FROM sys.indexes
            WHERE name = 'UX_OptionSnapshotCalc_Snapshot'
              AND object_id = OBJECT_ID('{full_name}')
        )
        BEGIN
            CREATE UNIQUE INDEX UX_OptionSnapshotCalc_Snapshot
            ON {safe_calc}(option_snapshot_id);
        END;
        """
    )
    db.conn.commit()
    cursor.close()


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
    q: float = 0.0,
) -> float:
    if S <= 0 or K <= 0:
        return 0.0

    if T <= 0 or sigma <= 0:
        if option_type == "CE":
            return max(S - K, 0.0)
        if option_type == "PE":
            return max(K - S, 0.0)
        raise ValueError(f"Unsupported option_type: {option_type}")

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if option_type == "CE":
        return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    if option_type == "PE":
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)
    raise ValueError(f"Unsupported option_type: {option_type}")


def bs_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
    q: float = 0.0,
) -> dict[str, float | None]:
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = norm_pdf(d1)
    exp_qT = math.exp(-q * T)
    exp_rT = math.exp(-r * T)

    gamma = exp_qT * pdf_d1 / (S * sigma * sqrt_T)
    vega = S * exp_qT * pdf_d1 * sqrt_T * 0.01

    if option_type == "CE":
        delta = exp_qT * norm_cdf(d1)
        theta_annual = (
            -S * exp_qT * pdf_d1 * sigma / (2.0 * sqrt_T)
            - r * K * exp_rT * norm_cdf(d2)
            + q * S * exp_qT * norm_cdf(d1)
        )
    elif option_type == "PE":
        delta = -exp_qT * norm_cdf(-d1)
        theta_annual = (
            -S * exp_qT * pdf_d1 * sigma / (2.0 * sqrt_T)
            + r * K * exp_rT * norm_cdf(-d2)
            - q * S * exp_qT * norm_cdf(-d1)
        )
    else:
        raise ValueError(f"Unsupported option_type: {option_type}")

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta_annual / 365.0,
        "vega": vega,
    }


def option_lower_bound(
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str,
    q: float = 0.0,
) -> float:
    if option_type == "CE":
        return max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    if option_type == "PE":
        return max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0)
    raise ValueError(f"Unsupported option_type: {option_type}")


def implied_volatility_bisection(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str,
    q: float = 0.0,
    low: float = 0.0001,
    high: float = 5.0,
    tolerance: float = 1e-6,
    max_iter: int = 100,
) -> float | None:
    if market_price is None or market_price <= 0:
        return None
    if S <= 0 or K <= 0 or T <= 0:
        return None

    lower_bound = option_lower_bound(S, K, T, r, option_type, q)
    if market_price < lower_bound - 1.0:
        return None

    price_low = bs_price(S, K, T, r, low, option_type, q)
    price_high = bs_price(S, K, T, r, high, option_type, q)
    if market_price < price_low - 1e-6:
        return None
    if market_price > price_high + 1e-6:
        return None

    lo = low
    hi = high
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        model_price = bs_price(S, K, T, r, mid, option_type, q)
        diff = model_price - market_price
        if abs(diff) <= tolerance:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def calculate_days_to_expiry(snapshot_time: datetime, expiry_date: date) -> float:
    expiry_dt = datetime.combine(expiry_date, time(15, 30))
    seconds = (expiry_dt - snapshot_time).total_seconds()
    return max(seconds / 86400.0, 0.0)


def choose_valuation_price(row: dict[str, Any]) -> dict[str, float | None]:
    last_price = row.get("last_price")
    bid_price = row.get("bid_price")
    ask_price = row.get("ask_price")

    if (
        bid_price is not None
        and ask_price is not None
        and bid_price > 0
        and ask_price > 0
        and ask_price >= bid_price
    ):
        mid_price = (bid_price + ask_price) / 2.0
        spread_width = ask_price - bid_price
        spread_width_pct = spread_width / mid_price if mid_price > 0 else None
        return {
            "valuation_price": mid_price,
            "mid_price": mid_price,
            "spread_width": spread_width,
            "spread_width_pct": spread_width_pct,
        }

    if last_price is not None and last_price > 0:
        return {
            "valuation_price": float(last_price),
            "mid_price": None,
            "spread_width": None,
            "spread_width_pct": None,
        }

    return {
        "valuation_price": None,
        "mid_price": None,
        "spread_width": None,
        "spread_width_pct": None,
    }


def calculate_snapshot_calc(row: dict[str, Any], settings: Settings) -> dict[str, Any]:
    price_info = choose_valuation_price(row)
    calc = {
        "option_snapshot_id": row["snapshot_id"],
        "implied_volatility": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "valuation_price": price_info["valuation_price"],
        "intrinsic_value": None,
        "time_value": None,
        "mid_price": price_info["mid_price"],
        "spread_width": price_info["spread_width"],
        "spread_width_pct": price_info["spread_width_pct"],
        "days_to_expiry": None,
        "risk_free_rate": settings.risk_free_rate,
        "calculation_status": "OK",
        "calculation_error": None,
        "created_at": datetime.utcnow(),
    }

    try:
        S = row.get("underlying_price")
        K = row.get("strike")
        option_type = row.get("instrument_type")
        snapshot_time = row.get("snapshot_time")
        expiry_date = row.get("expiry")
        valuation_price = calc["valuation_price"]

        if S is None or S <= 0:
            calc["calculation_status"] = "INVALID_UNDERLYING_PRICE"
            return calc
        if K is None or K <= 0:
            calc["calculation_status"] = "INVALID_STRIKE"
            return calc
        if valuation_price is None or valuation_price <= 0:
            calc["calculation_status"] = "INVALID_VALUATION_PRICE"
            return calc
        if option_type not in ("CE", "PE"):
            calc["calculation_status"] = f"INVALID_OPTION_TYPE_{option_type}"
            return calc

        intrinsic_value = max(S - K, 0.0) if option_type == "CE" else max(K - S, 0.0)
        days_to_expiry = calculate_days_to_expiry(snapshot_time, expiry_date)
        T = days_to_expiry / 365.0
        calc["intrinsic_value"] = intrinsic_value
        calc["time_value"] = valuation_price - intrinsic_value
        calc["days_to_expiry"] = days_to_expiry

        if T <= 0:
            calc["calculation_status"] = "EXPIRED_OR_ZERO_DTE"
            return calc

        iv = implied_volatility_bisection(
            market_price=valuation_price,
            S=S,
            K=K,
            T=T,
            r=settings.risk_free_rate,
            option_type=option_type,
            q=settings.dividend_yield,
        )
        if iv is None:
            calc["calculation_status"] = "IV_NOT_SOLVABLE"
            return calc

        greeks = bs_greeks(
            S=S,
            K=K,
            T=T,
            r=settings.risk_free_rate,
            sigma=iv,
            option_type=option_type,
            q=settings.dividend_yield,
        )
        calc["implied_volatility"] = iv
        calc["delta"] = greeks["delta"]
        calc["gamma"] = greeks["gamma"]
        calc["theta"] = greeks["theta"]
        calc["vega"] = greeks["vega"]
        return calc

    except Exception as exc:
        calc["calculation_status"] = "ERROR"
        calc["calculation_error"] = str(exc)[:500]
        return calc


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def load_snapshot_rows_to_calculate(
    db: DatabaseClient,
    settings: Settings,
    from_date: date,
    to_date: date,
    recompute: bool,
    batch_size: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    snapshot_table = safe_table_name(settings.option_snapshot_table)
    instrument_table = safe_table_name(settings.option_instrument_table)
    calc_table = safe_table_name(settings.option_snapshot_calc_table)
    calc_filter = "" if recompute else "AND c.option_snapshot_id IS NULL"

    sql = f"""
        SELECT
            s.id AS snapshot_id,
            s.option_instrument_id,
            s.snapshot_time,
            s.trade_date,
            s.snapshot_label,
            s.underlying_price,
            s.last_price,
            s.bid_price,
            s.ask_price,
            i.strike,
            CAST(i.expiry AS date) AS expiry,
            i.instrument_type
        FROM {snapshot_table} s
        INNER JOIN {instrument_table} i
            ON i.id = s.option_instrument_id
        LEFT JOIN {calc_table} c
            ON c.option_snapshot_id = s.id
        WHERE s.trade_date >= ?
          AND s.trade_date <= ?
          {calc_filter}
        ORDER BY s.trade_date, s.snapshot_label, s.id
        OFFSET ? ROWS FETCH NEXT ? ROWS ONLY;
    """
    rows = db.conn.cursor().execute(sql, from_date, to_date, offset, batch_size).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append({
            "snapshot_id": int(row.snapshot_id),
            "option_instrument_id": int(row.option_instrument_id),
            "snapshot_time": row.snapshot_time,
            "trade_date": _as_date(row.trade_date),
            "snapshot_label": row.snapshot_label,
            "underlying_price": float(row.underlying_price) if row.underlying_price is not None else None,
            "last_price": float(row.last_price) if row.last_price is not None else None,
            "bid_price": float(row.bid_price) if row.bid_price is not None else None,
            "ask_price": float(row.ask_price) if row.ask_price is not None else None,
            "strike": float(row.strike) if row.strike is not None else None,
            "expiry": _as_date(row.expiry),
            "instrument_type": row.instrument_type,
        })
    return output


def load_snapshot_rows_by_ids(
    db: DatabaseClient,
    settings: Settings,
    snapshot_ids: list[int],
) -> list[dict[str, Any]]:
    if not snapshot_ids:
        return []

    is_postgres = getattr(db, "db_kind", "") == "postgres"
    snapshot_table = pg_table_name(settings.option_snapshot_table) if is_postgres else safe_table_name(settings.option_snapshot_table)
    instrument_table = pg_table_name(settings.option_instrument_table) if is_postgres else safe_table_name(settings.option_instrument_table)
    placeholders = ",".join("%s" if is_postgres else "?" for _ in snapshot_ids)

    sql = f"""
        SELECT
            s.id AS snapshot_id,
            s.option_instrument_id,
            s.snapshot_time,
            s.trade_date,
            s.snapshot_label,
            s.underlying_price,
            s.last_price,
            s.bid_price,
            s.ask_price,
            i.strike,
            CAST(i.expiry AS date) AS expiry,
            i.instrument_type
        FROM {snapshot_table} s
        INNER JOIN {instrument_table} i
            ON i.id = s.option_instrument_id
        WHERE s.id IN ({placeholders})
        ORDER BY s.trade_date, s.snapshot_label, s.id;
    """
    cur = db.conn.cursor()
    cur.execute(sql, snapshot_ids)
    rows = cur.fetchall()
    cur.close()
    output: list[dict[str, Any]] = []
    for row in rows:
        if is_postgres:
            snapshot_id, option_instrument_id, snapshot_time, trade_date, snapshot_label, underlying_price, last_price, bid_price, ask_price, strike, expiry, instrument_type = row
        else:
            snapshot_id = row.snapshot_id
            option_instrument_id = row.option_instrument_id
            snapshot_time = row.snapshot_time
            trade_date = row.trade_date
            snapshot_label = row.snapshot_label
            underlying_price = row.underlying_price
            last_price = row.last_price
            bid_price = row.bid_price
            ask_price = row.ask_price
            strike = row.strike
            expiry = row.expiry
            instrument_type = row.instrument_type
        output.append({
            "snapshot_id": int(snapshot_id),
            "option_instrument_id": int(option_instrument_id),
            "snapshot_time": snapshot_time,
            "trade_date": _as_date(trade_date),
            "snapshot_label": snapshot_label,
            "underlying_price": float(underlying_price) if underlying_price is not None else None,
            "last_price": float(last_price) if last_price is not None else None,
            "bid_price": float(bid_price) if bid_price is not None else None,
            "ask_price": float(ask_price) if ask_price is not None else None,
            "strike": float(strike) if strike is not None else None,
            "expiry": _as_date(expiry),
            "instrument_type": instrument_type,
        })
    return output


def upsert_calc_row(db: DatabaseClient, settings: Settings, calc: dict[str, Any]) -> None:
    if getattr(db, "db_kind", "") == "postgres":
        calc_table = pg_table_name(settings.option_snapshot_calc_table)
        sql = f"""
            INSERT INTO {calc_table}
            (
                option_snapshot_id,
                implied_volatility,
                delta,
                gamma,
                theta,
                vega,
                valuation_price,
                intrinsic_value,
                time_value,
                mid_price,
                spread_width,
                spread_width_pct,
                days_to_expiry,
                risk_free_rate,
                calculation_status,
                calculation_error,
                created_at
            )
            VALUES
            (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (option_snapshot_id) DO UPDATE SET
                implied_volatility = EXCLUDED.implied_volatility,
                delta = EXCLUDED.delta,
                gamma = EXCLUDED.gamma,
                theta = EXCLUDED.theta,
                vega = EXCLUDED.vega,
                valuation_price = EXCLUDED.valuation_price,
                intrinsic_value = EXCLUDED.intrinsic_value,
                time_value = EXCLUDED.time_value,
                mid_price = EXCLUDED.mid_price,
                spread_width = EXCLUDED.spread_width,
                spread_width_pct = EXCLUDED.spread_width_pct,
                days_to_expiry = EXCLUDED.days_to_expiry,
                risk_free_rate = EXCLUDED.risk_free_rate,
                calculation_status = EXCLUDED.calculation_status,
                calculation_error = EXCLUDED.calculation_error,
                created_at = EXCLUDED.created_at
        """
        db.conn.cursor().execute(
            sql,
            [
                calc["option_snapshot_id"],
                calc["implied_volatility"],
                calc["delta"],
                calc["gamma"],
                calc["theta"],
                calc["vega"],
                calc["valuation_price"],
                calc["intrinsic_value"],
                calc["time_value"],
                calc["mid_price"],
                calc["spread_width"],
                calc["spread_width_pct"],
                calc["days_to_expiry"],
                calc["risk_free_rate"],
                calc["calculation_status"],
                calc["calculation_error"],
                calc["created_at"],
            ],
        )
        return

    calc_table = safe_table_name(settings.option_snapshot_calc_table)
    sql = f"""
        UPDATE {calc_table}
        SET
            implied_volatility = ?,
            delta = ?,
            gamma = ?,
            theta = ?,
            vega = ?,
            valuation_price = ?,
            intrinsic_value = ?,
            time_value = ?,
            mid_price = ?,
            spread_width = ?,
            spread_width_pct = ?,
            days_to_expiry = ?,
            risk_free_rate = ?,
            calculation_status = ?,
            calculation_error = ?,
            created_at = ?
        WHERE option_snapshot_id = ?;

        IF @@ROWCOUNT = 0
        BEGIN
            INSERT INTO {calc_table}
            (
                option_snapshot_id,
                implied_volatility,
                delta,
                gamma,
                theta,
                vega,
                valuation_price,
                intrinsic_value,
                time_value,
                mid_price,
                spread_width,
                spread_width_pct,
                days_to_expiry,
                risk_free_rate,
                calculation_status,
                calculation_error,
                created_at
            )
            VALUES
            (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            );
        END
    """
    update_params = [
        calc["implied_volatility"],
        calc["delta"],
        calc["gamma"],
        calc["theta"],
        calc["vega"],
        calc["valuation_price"],
        calc["intrinsic_value"],
        calc["time_value"],
        calc["mid_price"],
        calc["spread_width"],
        calc["spread_width_pct"],
        calc["days_to_expiry"],
        calc["risk_free_rate"],
        calc["calculation_status"],
        calc["calculation_error"],
        calc["created_at"],
        calc["option_snapshot_id"],
    ]
    insert_params = [
        calc["option_snapshot_id"],
        calc["implied_volatility"],
        calc["delta"],
        calc["gamma"],
        calc["theta"],
        calc["vega"],
        calc["valuation_price"],
        calc["intrinsic_value"],
        calc["time_value"],
        calc["mid_price"],
        calc["spread_width"],
        calc["spread_width_pct"],
        calc["days_to_expiry"],
        calc["risk_free_rate"],
        calc["calculation_status"],
        calc["calculation_error"],
        calc["created_at"],
    ]
    db.conn.cursor().execute(sql, update_params + insert_params)


def calculate_snapshot_ids(
    db: DatabaseClient,
    settings: Settings,
    snapshot_ids: list[int],
    batch_size: int = 1000,
) -> dict[str, int]:
    unique_snapshot_ids = list(dict.fromkeys(int(snapshot_id) for snapshot_id in snapshot_ids))
    total_processed = total_ok = total_non_ok = total_error = 0
    if not unique_snapshot_ids:
        return {
            "rows_processed": 0,
            "ok": 0,
            "non_ok": 0,
            "errors": 0,
        }

    ensure_calc_index(db, settings)
    for start in range(0, len(unique_snapshot_ids), batch_size):
        batch_ids = unique_snapshot_ids[start:start + batch_size]
        rows = load_snapshot_rows_by_ids(db, settings, batch_ids)
        for row in rows:
            calc = calculate_snapshot_calc(row, settings)
            upsert_calc_row(db, settings, calc)
            total_processed += 1
            if calc["calculation_status"] == "OK":
                total_ok += 1
            elif calc["calculation_status"] == "ERROR":
                total_error += 1
            else:
                total_non_ok += 1
        db.conn.commit()

    result = {
        "rows_processed": total_processed,
        "ok": total_ok,
        "non_ok": total_non_ok,
        "errors": total_error,
    }
    print(f"OptionSnapshotCalc updated for snapshot_ids={len(unique_snapshot_ids)} | {result}")
    return result


def calculate_all(
    from_date: date,
    to_date: date,
    settings: Settings,
    batch_size: int = 1000,
    recompute: bool = False,
    max_recompute_rows: int = 100000,
) -> dict[str, int]:
    db = get_database_client(get_settings())
    db.connect()
    total_processed = total_ok = total_non_ok = total_error = 0
    offset = 0
    try:
        ensure_calc_index(db, settings)
        while True:
            rows = load_snapshot_rows_to_calculate(
                db=db,
                settings=settings,
                from_date=from_date,
                to_date=to_date,
                recompute=recompute,
                batch_size=batch_size,
                offset=offset if recompute else 0,
            )
            if not rows:
                break

            for row in rows:
                calc = calculate_snapshot_calc(row, settings)
                upsert_calc_row(db, settings, calc)
                total_processed += 1
                if calc["calculation_status"] == "OK":
                    total_ok += 1
                elif calc["calculation_status"] == "ERROR":
                    total_error += 1
                else:
                    total_non_ok += 1

            db.conn.commit()
            print(
                f"Processed batch={len(rows)} | total={total_processed} | "
                f"OK={total_ok} | Non-OK={total_non_ok} | Errors={total_error}"
            )

            if recompute:
                offset += batch_size
                if total_processed >= max_recompute_rows:
                    print("Stopped due to max_recompute_rows safety limit.")
                    break
    finally:
        db.close()

    result = {
        "rows_processed": total_processed,
        "ok": total_ok,
        "non_ok": total_non_ok,
        "errors": total_error,
    }
    print("")
    print("OptionSnapshotCalc calculation completed.")
    print(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate dbo.OptionSnapshotCalc from dbo.OptionSnapshot.")
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--snapshot-table", default="dbo.OptionSnapshot")
    parser.add_argument("--instrument-table", default="dbo.OptionInstrument")
    parser.add_argument("--calc-table", default="dbo.OptionSnapshotCalc")
    parser.add_argument("--risk-free-rate", type=float, default=0.06)
    parser.add_argument("--dividend-yield", type=float, default=0.00)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--recompute", action="store_true")
    parser.add_argument("--max-recompute-rows", type=int, default=100000)
    args = parser.parse_args()

    settings = Settings(
        option_snapshot_table=args.snapshot_table,
        option_instrument_table=args.instrument_table,
        option_snapshot_calc_table=args.calc_table,
        risk_free_rate=args.risk_free_rate,
        dividend_yield=args.dividend_yield,
    )
    calculate_all(
        from_date=date.fromisoformat(args.from_date),
        to_date=date.fromisoformat(args.to_date),
        settings=settings,
        batch_size=args.batch_size,
        recompute=args.recompute,
        max_recompute_rows=args.max_recompute_rows,
    )


if __name__ == "__main__":
    main()
