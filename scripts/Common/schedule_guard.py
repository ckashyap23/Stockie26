from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> date:
    return datetime.now(IST).date()


def is_weekend(value: date) -> bool:
    return value.weekday() >= 5


def skip_if_weekend(job_name: str, run_date: date | None = None) -> bool:
    run_date = run_date or today_ist()
    if not is_weekend(run_date):
        return False

    print(
        f"{job_name}: skipping scheduled run on weekend "
        f"({run_date.isoformat()}, {run_date.strftime('%A')})."
    )
    return True
