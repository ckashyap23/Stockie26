"""Load point-in-time-safe macro source values into MacroFactorDaily."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from io import StringIO
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client


MARKET_TICKERS = {
    "BZ=F": "crude_close",
    "^TNX": "us10y_level",
    "INR=X": "usdinr_close",
}
NSE_HOME_URL = "https://www.nseindia.com"
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_PARTICIPANT_OI_URL = (
    "https://nsearchives.nseindia.com/content/nsccl/"
    "fao_participant_oi_{date}.csv"
)
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/fiidii-market-activity",
}

MACRO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS "MacroFactorDaily" (
    factor_date date PRIMARY KEY,
    india_vix double precision,
    source_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz
);
ALTER TABLE "MacroFactorDaily"
    ADD COLUMN IF NOT EXISTS crude_close double precision,
    ADD COLUMN IF NOT EXISTS us10y_level double precision,
    ADD COLUMN IF NOT EXISTS usdinr_close double precision,
    ADD COLUMN IF NOT EXISTS fii_net_cr double precision,
    ADD COLUMN IF NOT EXISTS dii_net_cr double precision,
    ADD COLUMN IF NOT EXISTS fii_idx_fut_long bigint,
    ADD COLUMN IF NOT EXISTS fii_idx_fut_short bigint;
"""


def _none_if_missing(value: Any) -> Any:
    return None if value is None or pd.isna(value) else value


def fetch_market_macro_closes(start_date: date, end_date: date) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for macro market loading") from exc

    data = yf.download(
        list(MARKET_TICKERS),
        start=(start_date - timedelta(days=10)).isoformat(),
        end=(end_date + timedelta(days=2)).isoformat(),
        auto_adjust=False,
        progress=False,
        group_by="column",
        threads=False,
    )
    if data.empty:
        raise RuntimeError("Yahoo Finance returned no macro market data")

    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
    rows = pd.DataFrame(index=pd.to_datetime(close.index).tz_localize(None))
    for ticker, column in MARKET_TICKERS.items():
        if ticker not in close.columns:
            rows[column] = pd.NA
        else:
            rows[column] = pd.to_numeric(close[ticker], errors="coerce")
    rows.index.name = "factor_date"
    rows = rows.reset_index()
    rows["factor_date"] = rows["factor_date"].dt.date
    return rows[rows["factor_date"].between(start_date, end_date)].reset_index(drop=True)


def _parse_number(value: Any) -> float | None:
    if value is None or str(value).strip() in {"", "-", "NA", "N/A"}:
        return None
    return float(str(value).replace(",", "").strip())


def normalize_fii_dii_records(records: list[dict[str, Any]]) -> pd.DataFrame:
    normalized: dict[date, dict[str, Any]] = {}
    for record in records:
        canonical = {
            "".join(character for character in str(key).lower() if character.isalnum()): value
            for key, value in record.items()
        }
        raw_date = (
            canonical.get("date")
            or canonical.get("tradedate")
            or canonical.get("reportdate")
        )
        category = str(
            canonical.get("category")
            or canonical.get("categoryname")
            or canonical.get("clienttype")
            or ""
        ).upper()
        if not raw_date or not category:
            continue
        parsed_date = pd.to_datetime(raw_date, dayfirst=True, errors="coerce")
        if pd.isna(parsed_date):
            continue
        factor_date = parsed_date.date()
        net = _parse_number(
            canonical.get("netvalue")
            or canonical.get("netvaluerscr")
            or canonical.get("netpurchasesales")
            or canonical.get("netpurchaseorsales")
        )
        if net is None:
            buy = _parse_number(
                canonical.get("buyvalue")
                or canonical.get("buyvaluerscr")
            )
            sell = _parse_number(
                canonical.get("sellvalue")
                or canonical.get("sellvaluerscr")
            )
            net = buy - sell if buy is not None and sell is not None else None
        row = normalized.setdefault(
            factor_date,
            {"factor_date": factor_date, "fii_net_cr": None, "dii_net_cr": None},
        )
        if "FII" in category or "FPI" in category:
            row["fii_net_cr"] = net
        elif "DII" in category:
            row["dii_net_cr"] = net
    return pd.DataFrame(normalized.values()).sort_values("factor_date").reset_index(drop=True)


def fetch_nse_fii_dii(start_date: date, end_date: date) -> pd.DataFrame:
    with requests.Session() as session:
        session.headers.update(NSE_HEADERS)
        home = session.get(NSE_HOME_URL, timeout=20)
        home.raise_for_status()
        response = session.get(NSE_FII_DII_URL, timeout=20)
        response.raise_for_status()
        payload = response.json()
    records = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise RuntimeError("Unexpected NSE FII/DII response shape")
    frame = normalize_fii_dii_records(records)
    if frame.empty:
        raise RuntimeError("NSE returned no parseable FII/DII rows")
    filtered = frame[frame["factor_date"].between(start_date, end_date)].reset_index(drop=True)
    if filtered.empty:
        raise RuntimeError(
            f"NSE returned no FII/DII rows in {start_date} through {end_date}; "
            "use --fii-dii-csv with an official archive export for historical backfills"
        )
    return filtered


def load_fii_dii_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"FII/DII CSV not found: {path}")
    frame = pd.read_csv(path)
    return normalize_fii_dii_records(frame.to_dict("records"))


def participant_oi_archive_url(factor_date: date) -> str:
    return NSE_PARTICIPANT_OI_URL.format(date=factor_date.strftime("%d%m%Y"))


def normalize_participant_oi_csv(content: str | bytes, factor_date: date) -> pd.DataFrame:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    frame = pd.read_csv(StringIO(text), skiprows=1)
    frame.columns = [
        "".join(character for character in str(column).lower() if character.isalnum())
        for column in frame.columns
    ]
    required = {"clienttype", "futureindexlong", "futureindexshort"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(
            f"Participant OI archive for {factor_date} is missing columns: "
            f"{', '.join(missing)}"
        )
    fii = frame[
        frame["clienttype"].astype(str).str.strip().str.upper().eq("FII")
    ]
    if len(fii) != 1:
        raise ValueError(
            f"Participant OI archive for {factor_date} contains {len(fii)} FII rows"
        )
    long_value = _parse_number(fii.iloc[0]["futureindexlong"])
    short_value = _parse_number(fii.iloc[0]["futureindexshort"])
    if long_value is None or short_value is None:
        raise ValueError(f"Participant OI archive for {factor_date} has blank FII values")
    if long_value < 0 or short_value < 0:
        raise ValueError(f"Participant OI archive for {factor_date} has negative FII values")
    if not long_value.is_integer() or not short_value.is_integer():
        raise ValueError(
            f"Participant OI archive for {factor_date} has non-integral FII contracts"
        )
    return pd.DataFrame(
        [
            {
                "factor_date": factor_date,
                "fii_idx_fut_long": int(long_value),
                "fii_idx_fut_short": int(short_value),
            }
        ]
    )


def fetch_nse_participant_oi(factor_date: date) -> pd.DataFrame:
    response = requests.get(
        participant_oi_archive_url(factor_date),
        headers={
            **NSE_HEADERS,
            "Accept": "text/csv,*/*",
            "Referer": "https://www.nseindia.com/all-reports-derivatives",
        },
        timeout=30,
    )
    if response.status_code == 404:
        raise FileNotFoundError(
            f"NSE participant OI archive is unavailable for {factor_date}"
        )
    response.raise_for_status()
    return normalize_participant_oi_csv(response.content, factor_date)


def fetch_nse_participant_oi_dates(
    trading_dates: Iterable[date],
    max_workers: int = 4,
) -> tuple[pd.DataFrame, list[date]]:
    dates = sorted(set(trading_dates))
    if not dates:
        return (
            pd.DataFrame(
                columns=["factor_date", "fii_idx_fut_long", "fii_idx_fut_short"]
            ),
            [],
        )
    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")

    rows: list[pd.DataFrame] = []
    missing: list[date] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_nse_participant_oi, factor_date): factor_date
            for factor_date in dates
        }
        for future in as_completed(futures):
            factor_date = futures[future]
            try:
                rows.append(future.result())
            except FileNotFoundError:
                missing.append(factor_date)
            except Exception as exc:
                failures.append(f"{factor_date}: {exc}")
    if failures:
        sample = "; ".join(sorted(failures)[:5])
        raise RuntimeError(
            f"Failed to fetch {len(failures)} NSE participant OI archives: {sample}"
        )
    result = (
        pd.concat(rows, ignore_index=True).sort_values("factor_date").reset_index(drop=True)
        if rows
        else pd.DataFrame(
            columns=["factor_date", "fii_idx_fut_long", "fii_idx_fut_short"]
        )
    )
    return result, sorted(missing)


def upsert_macro_factors(rows: pd.DataFrame) -> int:
    if rows.empty:
        return 0
    settings = get_settings()
    db = get_database_client(settings)
    if getattr(db, "db_kind", "") != "postgres":
        raise RuntimeError("Macro factor loading currently supports Supabase/Postgres only")
    db.connect()
    try:
        from psycopg2.extras import execute_values

        with db.conn.cursor() as cur:
            cur.execute(MACRO_SCHEMA_SQL)
            values = []
            for record in rows.to_dict("records"):
                market_values = [
                    record.get("crude_close"),
                    record.get("us10y_level"),
                    record.get("usdinr_close"),
                ]
                source = {
                    "market": (
                        "yfinance"
                        if any(_none_if_missing(value) is not None for value in market_values)
                        else None
                    ),
                    "fii_dii": _none_if_missing(record.get("fii_dii_source")),
                    "participant_oi": _none_if_missing(
                        record.get("participant_oi_source")
                    ),
                }
                source = {key: value for key, value in source.items() if value is not None}
                values.append(
                    (
                        record["factor_date"],
                        _none_if_missing(record.get("crude_close")),
                        _none_if_missing(record.get("us10y_level")),
                        _none_if_missing(record.get("usdinr_close")),
                        _none_if_missing(record.get("fii_net_cr")),
                        _none_if_missing(record.get("dii_net_cr")),
                        _none_if_missing(record.get("fii_idx_fut_long")),
                        _none_if_missing(record.get("fii_idx_fut_short")),
                        json.dumps(source, allow_nan=False),
                    )
                )
            execute_values(
                cur,
                """
                INSERT INTO "MacroFactorDaily"
                    (factor_date, crude_close, us10y_level, usdinr_close,
                     fii_net_cr, dii_net_cr, fii_idx_fut_long,
                     fii_idx_fut_short, source_json, updated_at)
                VALUES %s
                ON CONFLICT (factor_date) DO UPDATE SET
                    crude_close = COALESCE(EXCLUDED.crude_close, "MacroFactorDaily".crude_close),
                    us10y_level = COALESCE(EXCLUDED.us10y_level, "MacroFactorDaily".us10y_level),
                    usdinr_close = COALESCE(EXCLUDED.usdinr_close, "MacroFactorDaily".usdinr_close),
                    fii_net_cr = COALESCE(EXCLUDED.fii_net_cr, "MacroFactorDaily".fii_net_cr),
                    dii_net_cr = COALESCE(EXCLUDED.dii_net_cr, "MacroFactorDaily".dii_net_cr),
                    fii_idx_fut_long = COALESCE(
                        EXCLUDED.fii_idx_fut_long,
                        "MacroFactorDaily".fii_idx_fut_long
                    ),
                    fii_idx_fut_short = COALESCE(
                        EXCLUDED.fii_idx_fut_short,
                        "MacroFactorDaily".fii_idx_fut_short
                    ),
                    source_json = COALESCE("MacroFactorDaily".source_json, '{}'::jsonb)
                                  || EXCLUDED.source_json,
                    updated_at = now()
                """,
                [value + (None,) for value in values],
            )
        db.conn.commit()
    finally:
        db.close()
    return len(rows)


def run_macro_factor_load(
    start_date: date,
    end_date: date,
    fii_dii_csv: Path | None = None,
) -> dict[str, Any]:
    market = fetch_market_macro_closes(start_date, end_date)
    flow_error = None
    try:
        flows = load_fii_dii_csv(fii_dii_csv) if fii_dii_csv else fetch_nse_fii_dii(start_date, end_date)
        flows["fii_dii_source"] = "nse_csv" if fii_dii_csv else "nse_api"
    except Exception as exc:
        flow_error = str(exc)
        print(f"[WARN] FII/DII load failed; market macro rows will still be stored: {exc}")
        flows = pd.DataFrame(columns=["factor_date", "fii_net_cr", "dii_net_cr", "fii_dii_source"])

    participant_error = None
    participant_missing: list[date] = []
    try:
        participant, participant_missing = fetch_nse_participant_oi_dates(
            pd.date_range(start_date, end_date, freq="B").date,
            max_workers=1,
        )
        participant["participant_oi_source"] = "nse_archive"
        if participant_missing:
            participant_error = (
                "NSE participant OI archive unavailable for: "
                + ", ".join(value.isoformat() for value in participant_missing)
            )
            print(f"[WARN] {participant_error}")
    except Exception as exc:
        participant_error = str(exc)
        print(f"[WARN] Participant OI load failed; other macro rows will still be stored: {exc}")
        participant = pd.DataFrame(
            columns=[
                "factor_date",
                "fii_idx_fut_long",
                "fii_idx_fut_short",
                "participant_oi_source",
            ]
        )

    rows = (
        market.merge(flows, on="factor_date", how="outer")
        .merge(participant, on="factor_date", how="outer")
        .sort_values("factor_date")
    )
    count = upsert_macro_factors(rows)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "rows_upserted": count,
        "fii_dii_error": flow_error,
        "participant_oi_rows": len(participant),
        "participant_oi_error": participant_error,
    }
