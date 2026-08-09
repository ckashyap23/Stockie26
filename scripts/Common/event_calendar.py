"""Forward-looking macro event calendar for NIFTY signal suppression.

Purpose: given a calendar month, return the dates on which a scheduled macro
event is expected to move the Indian market, so the cascade can suppress or
downsize positions whose TRADE date (or holding window) lands on one.

Design notes
------------
- Dates are stored as the *NIFTY impact date*, not the raw event date:
    * RBI MPC decisions are announced ~10:00 IST on the final meeting day
      -> impact date = announcement day itself.
    * FOMC decisions land at 14:00 ET (~00:30 IST next day)
      -> impact date = the Indian session AFTER the decision day.
    * Union Budget is presented ~11:00 IST on Feb 1 (NSE holds a special
      session even when Feb 1 falls on a weekend — e.g. Sat 01-02-2025,
      Sun 01-02-2026) -> impact date = Feb 1 itself.
- The calendar is static and must be refreshed once a year when RBI/Fed
  publish schedules. Lookups outside maintained coverage raise
  EventCalendarCoverageError so a stale calendar fails LOUDLY instead of
  silently reporting "no events" (this module exists to veto trades — a
  silent empty answer is the dangerous failure mode).
- Sources (fetched 2026-07-15):
    RBI MPC FY2026-27 schedule (rbi.org.in via 5paisa summary)
    FOMC 2026 confirmed + 2027 tentative (federalreserve.gov via fedratecalc)

Update checklist (yearly, ~March):
  1. RBI publishes the next FY's MPC calendar with the annual policy.
  2. Fed publishes year N+1 tentative dates around June of year N.
  3. Add election-result days / any one-off events when announced.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

BUDGET = "UNION_BUDGET"
RBI_MPC = "RBI_MPC"
FOMC = "FOMC"
ONE_OFF = "ONE_OFF"          # election results, GST council megadecisions, etc.


class EventCalendarCoverageError(LookupError):
    """Raised when the requested month is outside maintained calendar coverage."""


@dataclass(frozen=True)
class MarketEvent:
    impact_date: date        # the NIFTY session expected to react
    event_type: str
    label: str


# ---------------------------------------------------------------------------
# Static calendars — NIFTY impact dates (see design notes for the shift rules).
# ---------------------------------------------------------------------------

_RBI_MPC_ANNOUNCEMENTS = [
    # FY2026-27 (meetings run 3 days; announcement on the last day)
    date(2026, 4, 8), date(2026, 6, 5), date(2026, 8, 5),
    date(2026, 10, 7), date(2026, 12, 4), date(2027, 2, 5),
]

_FOMC_DECISION_DATES = [
    # 2026 confirmed (decision = day 2 of the meeting, US time)
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
    # 2027 tentative — verify after each preceding meeting confirms it
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28), date(2027, 6, 9),
    date(2027, 7, 28), date(2027, 9, 15), date(2027, 10, 27), date(2027, 12, 8),
]

_BUDGET_DATES = [
    date(2027, 2, 1),  # Union Budget FY2027-28 (Monday)
    # NOTE: add interim-budget dates in general-election years when announced.
]

_ONE_OFF_EVENTS: list[tuple[date, str]] = [
    # (date, label) — add state/general election result days, index rejigs, etc.
]

# Coverage window: months we consider fully maintained. Requests outside this
# range raise EventCalendarCoverageError.
_COVERAGE_START = date(2026, 1, 1)
_COVERAGE_END = date(2027, 12, 31)


def _build_events() -> list[MarketEvent]:
    events: list[MarketEvent] = []
    for d in _RBI_MPC_ANNOUNCEMENTS:
        events.append(MarketEvent(d, RBI_MPC, f"RBI MPC decision {d:%d-%b-%Y} (10:00 IST)"))
    for d in _FOMC_DECISION_DATES:
        # 14:00 ET decision → ~00:30 IST next calendar day; roll over weekends.
        raw_impact = d + timedelta(days=1)
        if raw_impact.weekday() == 5:   # Saturday → Monday
            raw_impact += timedelta(days=2)
        elif raw_impact.weekday() == 6: # Sunday → Monday
            raw_impact += timedelta(days=1)
        impact = raw_impact
        events.append(MarketEvent(impact, FOMC, f"FOMC decision {d:%d-%b-%Y} (impact next IST session)"))
    for d in _BUDGET_DATES:
        events.append(MarketEvent(d, BUDGET, f"Union Budget {d:%d-%b-%Y} (~11:00 IST)"))
    for d, label in _ONE_OFF_EVENTS:
        events.append(MarketEvent(d, ONE_OFF, label))
    return sorted(events, key=lambda e: e.impact_date)


_ALL_EVENTS = _build_events()


def get_event_days(year: int, month: int) -> list[MarketEvent]:
    """All macro-event NIFTY impact dates in the given calendar month.

    Raises EventCalendarCoverageError if the month is outside the maintained
    window, so a stale calendar can never silently return "no events".
    """
    month_start = date(year, month, 1)
    if not (_COVERAGE_START <= month_start <= _COVERAGE_END):
        raise EventCalendarCoverageError(
            f"{year}-{month:02d} is outside maintained coverage "
            f"({_COVERAGE_START} .. {_COVERAGE_END}). Refresh the static "
            f"calendars in event_calendar.py before relying on it."
        )
    return [e for e in _ALL_EVENTS if e.impact_date.year == year and e.impact_date.month == month]


def is_event_impact_day(d: date, buffer_days: int = 0) -> bool:
    """True if `d` is (within `buffer_days` of) a macro-event impact date.

    Typical cascade use: signal fires on D, trade holds on D+1 — pass the
    TRADE date. buffer_days=1 also vetoes the session before/after the event
    (useful for multi-day holds over weekends).
    """
    get_event_days(d.year, d.month)  # coverage check, raises if stale
    return any(abs((e.impact_date - d).days) <= buffer_days for e in _ALL_EVENTS)


if __name__ == "__main__":
    import sys
    y = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
    m = int(sys.argv[2]) if len(sys.argv) > 2 else date.today().month
    for e in get_event_days(y, m):
        print(f"{e.impact_date}  [{e.event_type}]  {e.label}")
