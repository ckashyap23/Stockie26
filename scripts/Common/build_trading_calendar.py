from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.supabase_client import SupabaseDatabaseClient
from src.data_manager.kite_client import KiteClient

EXCHANGE = "NSE"


def get_calendar_sessions(start: date, end: date) -> set[date]:
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise SystemExit(
            "exchange-calendars is required for future trading-calendar generation.\n"
            "Install it with: pip install exchange-calendars"
        ) from exc

    calendar = xcals.get_calendar("XBOM")
    sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
    return {session.date() for session in sessions}


def last_thursday_of_month(year: int, month: int, trading_days: set[date]) -> date | None:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)

    while cursor.month == month:
        if cursor.weekday() == 3 and cursor in trading_days:
            return cursor
        cursor -= timedelta(days=1)
    return None


def resolve_underlying_token(db, underlying: str) -> int | None:
    watched = db.get_watched_instruments(instrument_type="INDEX")
    for item in watched:
        if item.tradingsymbol.upper() == underlying.upper() and item.instrument_token:
            return int(item.instrument_token)
    return None


def validate_with_kite(
    candidate_days: set[date],
    underlying: str,
    start: date,
    end: date,
) -> set[date]:
    settings = get_settings()
    db = SupabaseDatabaseClient(settings)
    db.connect()
    try:
        token = resolve_underlying_token(db, underlying)
    finally:
        db.close()

    if token is None:
        raise SystemExit(f"Could not resolve {underlying} instrument_token from WatchedInstrument.")

    kite_client = KiteClient(settings)
    kite_client.authenticate()

    validated: set[date] = set()
    for session in sorted(candidate_days):
        if session > date.today() or session < start or session > end:
            continue
        candles = kite_client.kite.historical_data(
            instrument_token=token,
            from_date=session,
            to_date=session + timedelta(days=1),
            interval="day",
        )
        if candles:
            validated.add(session)
    return validated


def build_rows(
    start: date,
    end: date,
    trading_days: set[date],
    notes_prefix: str,
) -> list[dict]:
    monthly_expiries: set[date] = set()
    for year in range(start.year, end.year + 1):
        for month in range(1, 13):
            expiry = last_thursday_of_month(year, month, trading_days)
            if expiry and start <= expiry <= end:
                monthly_expiries.add(expiry)

    rows: list[dict] = []
    cursor = start
    while cursor <= end:
        is_trading_day = cursor in trading_days
        rows.append({
            "calendar_date": cursor,
            "exchange": EXCHANGE,
            "is_trading_day": is_trading_day,
            "is_weekly_expiry": is_trading_day and cursor.weekday() == 3,
            "is_monthly_expiry": is_trading_day and cursor in monthly_expiries,
            "is_special_session": False,
            "notes": notes_prefix if is_trading_day else None,
        })
        cursor += timedelta(days=1)
    return rows


def run(
    start: date,
    end: date,
    underlying: str,
    validate_kite: bool,
) -> dict[str, int]:
    trading_days = get_calendar_sessions(start, end)
    notes = "source=exchange_calendars"

    if validate_kite:
        kite_days = validate_with_kite(trading_days, underlying, start, end)
        trading_days = (trading_days - {d for d in trading_days if d <= date.today()}) | kite_days
        notes = "source=exchange_calendars;kite_validated_historical"

    rows = build_rows(start, end, trading_days, notes)
    settings = get_settings()
    db = SupabaseDatabaseClient(settings)
    db.connect()
    try:
        upserted = db.upsert_trading_calendar(rows)
    finally:
        db.close()

    return {
        "calendar_rows": len(rows),
        "trading_days": sum(1 for row in rows if row["is_trading_day"]),
        "non_trading_days": sum(1 for row in rows if not row["is_trading_day"]),
        "upserted": upserted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate Supabase TradingCalendar for NSE sessions."
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--underlying", default="NIFTY", help="Kite validation underlying. Default: NIFTY")
    parser.add_argument(
        "--validate-with-kite",
        action="store_true",
        help="For historical dates, confirm trading sessions with Kite daily candles.",
    )
    args = parser.parse_args()

    result = run(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        underlying=args.underlying.upper(),
        validate_kite=args.validate_with_kite,
    )
    print(result)


if __name__ == "__main__":
    main()
