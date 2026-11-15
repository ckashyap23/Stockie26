"""Pre-open data-quality checks for the inputs of a morning decision.

For a decision on trading day N (taken from a snapshot at ~09:12 IST) the inputs
describe the previous Indian session T. This module reports, per field, whether
the value is OK, MISSING, STALE, NOT_FINAL or LATE so a downstream step can fall
back to Layer 1 only instead of silently using a repeated or half-formed value.

The checks are pure functions over pandas frames / dicts; ``load_inputs`` in
``scripts/daily_NIFTY/daily_data_quality_check.py`` does the DB reads.

Rules (see layer requirements):
  * a missing value is never filled with zero;
  * a value identical to the previous session's is STALE (no holiday exemption is
    needed: sources only emit rows for sessions that traded);
  * a row fetched before its market closed is NOT_FINAL;
  * on a live run, an input fetched after the decision time is LATE.
"""
from __future__ import annotations


from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

OK = "OK"
MISSING = "MISSING"
STALE = "STALE"
NOT_FINAL = "NOT_FINAL"
LATE = "LATE"

DECISION_TIME = time(9, 12)

US_INDEXES = ("SP500", "NASDAQ", "DOW", "RUSSELL2000")
EUROPE_INDEXES = ("FTSE100", "DAX", "CAC40")
ASIA_INDEXES = ("NIKKEI225", "HANG_SENG", "SHANGHAI", "KOSPI", "ASX200")
# Indexes the macro layer actually consumes; the rest are reported but optional.
REQUIRED_WESTERN = ("SP500", "NASDAQ")
MIN_ASIA_COVERAGE = 3

# A western daily row is only final once the session has closed. Conservative
# UTC close times (covering both daylight-saving regimes) on the session date.
_WESTERN_CLOSE_UTC = {**{c: time(21, 30) for c in US_INDEXES},
                      **{c: time(16, 30) for c in EUROPE_INDEXES}}

ASIA_PARTIAL_SOURCES = ("yfinance_5m_partial_0900", "yfinance_5m", "yfinance_1h_partial")
ASIA_EXACT_PARTIAL_SOURCE = "yfinance_5m_partial_0900"

MACRO_REQUIRED = ("brent_close", "us10y_level", "usdinr_close")
MACRO_OPTIONAL = ("wti_close", "india_vix", "fii_net_cr", "dii_net_cr")
DOMESTIC_OPTIONAL = (
    "pcr_oi", "oi_regime", "futures_oi",
    "breadth_advance_decline_ratio", "breadth_new_high_ratio",
    "banknifty_rel_strength",
)


@dataclass(frozen=True)
class Finding:
    field: str
    status: str
    required: bool
    detail: str = ""
    as_of: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass(frozen=True)
class PremarketInputs:
    decision_date: date            # N: the trading day being decided
    prev_session: date             # T: previous Indian session
    now: datetime | None           # IST wall clock; None disables the LATE check
    gift: Mapping[date, Mapping[str, Any]]      # trade_date -> {gift_0910, gift_1515, fetched_at}
    global_rows: pd.DataFrame      # GlobalIndexOhlc rows around N
    macro: pd.DataFrame            # MacroFactorDaily rows (factor_date + value columns)
    features: Mapping[str, Any] | None          # SignalFeatureDaily row for T (NIFTY), or None


def previous_weekday(d: date) -> date:
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def _isnull(value: Any) -> bool:
    return value is None or bool(pd.isna(value))


def _as_ist(value: Any) -> datetime | None:
    """Normalise a fetched_at to tz-aware IST; naive values are already IST."""
    if _isnull(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.to_pydatetime().replace(tzinfo=IST)
    return ts.to_pydatetime().astimezone(IST)


def _late(fetched_at: Any, decision_date: date, now: datetime | None) -> bool:
    """True when a live run finds an input fetched after the decision time."""
    if now is None:
        return False
    stamp = _as_ist(fetched_at)
    if stamp is None:
        return False
    return stamp > datetime.combine(decision_date, DECISION_TIME, tzinfo=IST)


def _iso(value: Any) -> str | None:
    stamp = _as_ist(value)
    return stamp.isoformat(timespec="seconds") if stamp else None


# ── GIFT ──────────────────────────────────────────────────────────────────────

def check_gift(inputs: PremarketInputs) -> list[Finding]:
    n, t = inputs.decision_date, inputs.prev_session
    today_row = inputs.gift.get(n) or {}
    prev_row = inputs.gift.get(t) or {}
    findings: list[Finding] = []

    g0910 = today_row.get("gift_0910")
    if _isnull(g0910):
        findings.append(Finding("gift_0910", MISSING, True, f"no 09:10 GIFT price for {n}"))
    else:
        fetched = today_row.get("fetched_at")
        before = prev_row.get("gift_0910")
        if not _isnull(before) and float(before) == float(g0910):
            findings.append(Finding("gift_0910", STALE, True, f"identical to {t} value {g0910}", _iso(fetched)))
        elif _late(fetched, n, inputs.now):
            findings.append(Finding("gift_0910", LATE, True, "fetched after the 09:12 decision time", _iso(fetched)))
        else:
            findings.append(Finding("gift_0910", OK, True, f"{float(g0910):.2f}", _iso(fetched)))

    g1515 = prev_row.get("gift_1515")
    if _isnull(g1515):
        findings.append(Finding("gift_1515_prev", MISSING, True, f"no 15:15 GIFT close for {t}"))
    else:
        findings.append(Finding("gift_1515_prev", OK, True, f"{float(g1515):.2f}"))
    return findings


# ── US / Europe daily sessions ────────────────────────────────────────────────

def check_western(inputs: PremarketInputs) -> list[Finding]:
    session = previous_weekday(inputs.decision_date)
    rows = inputs.global_rows
    findings: list[Finding] = []
    codes = US_INDEXES + EUROPE_INDEXES
    have = rows[rows["index_code"].isin(codes)] if not rows.empty else rows
    session_ts = pd.Timestamp(session)
    on_session = have[pd.to_datetime(have["trade_date"]) == session_ts] if not have.empty else have

    if on_session.empty:
        # Nothing at all for the session usually means an exchange holiday or a
        # failed load; the check cannot tell them apart, so say so.
        return [Finding(
            "western_session", MISSING, True,
            f"no US/Europe rows for session {session} (exchange holiday or load failed)",
        )]

    for code in codes:
        required = code in REQUIRED_WESTERN
        field = f"ohlc_{code}"
        mine = have[have["index_code"] == code].copy()
        mine["_d"] = pd.to_datetime(mine["trade_date"])
        today = mine[mine["_d"] == session_ts]
        if today.empty:
            findings.append(Finding(field, MISSING, required, f"no {session} session row"))
            continue
        row = today.sort_values("fetched_at").iloc[-1]
        if _isnull(row.get("open_price")) or _isnull(row.get("close_price")):
            findings.append(Finding(field, MISSING, required, "open/close is NULL"))
            continue
        if "is_final" in row and not _isnull(row["is_final"]) and not bool(row["is_final"]):
            findings.append(Finding(field, NOT_FINAL, required, "row is not final", _iso(row.get("fetched_at"))))
            continue
        stamp = _as_ist(row.get("fetched_at"))
        close_utc = datetime.combine(session, _WESTERN_CLOSE_UTC[code], tzinfo=timezone.utc)
        if stamp is not None and stamp.astimezone(timezone.utc) < close_utc:
            findings.append(Finding(
                field, NOT_FINAL, required,
                f"fetched {stamp:%Y-%m-%d %H:%M} IST before the {session} session closed",
                _iso(stamp),
            ))
            continue
        earlier = mine[mine["_d"] < session_ts].sort_values("_d")
        if not earlier.empty:
            prev = earlier.iloc[-1]
            if (float(prev["open_price"]) == float(row["open_price"])
                    and float(prev["close_price"]) == float(row["close_price"])):
                findings.append(Finding(field, STALE, required,
                                        f"open/close identical to {prev['_d'].date()}", _iso(stamp)))
                continue
        if _late(stamp, inputs.decision_date, inputs.now):
            findings.append(Finding(field, LATE, required, "fetched after the decision time", _iso(stamp)))
            continue
        findings.append(Finding(field, OK, required, f"{session}", _iso(stamp)))
    return findings


# ── Asia ──────────────────────────────────────────────────────────────────────

def check_asia(inputs: PremarketInputs) -> list[Finding]:
    n, t = inputs.decision_date, inputs.prev_session
    rows = inputs.global_rows
    findings: list[Finding] = []
    usable = 0
    for code in ASIA_INDEXES:
        mine = rows[rows["index_code"] == code].copy() if not rows.empty else rows
        if mine.empty:
            findings.append(Finding(f"asia_{code}", MISSING, False, "no rows"))
            continue
        mine["_d"] = pd.to_datetime(mine["trade_date"])
        partial = mine[(mine["_d"] == pd.Timestamp(n)) & (~mine["is_final"].fillna(True).astype(bool))]
        final = mine[(mine["_d"] <= pd.Timestamp(t)) & (mine["is_final"].fillna(True).astype(bool))]
        problems = []
        if partial.empty:
            problems.append(f"no pre-open partial for {n} (market holiday or load failed)")
        if final.empty or (pd.Timestamp(t) - final["_d"].max()) > pd.Timedelta(days=7):
            problems.append("no recent final close")
        if problems:
            findings.append(Finding(f"asia_{code}", MISSING, False, "; ".join(problems)))
            continue
        best = partial.sort_values("fetched_at").iloc[-1]
        stamp = _as_ist(best.get("fetched_at"))
        prior = mine[(mine["_d"] < pd.Timestamp(n)) & (~mine["is_final"].fillna(True).astype(bool))].sort_values("_d")
        if not prior.empty:
            p = prior.iloc[-1]
            if float(p["close_price"]) == float(best["close_price"]) and float(p["open_price"]) == float(best["open_price"]):
                findings.append(Finding(f"asia_{code}", STALE, False,
                                        f"partial identical to {p['_d'].date()}", _iso(stamp)))
                continue
        if _late(stamp, n, inputs.now):
            findings.append(Finding(f"asia_{code}", LATE, False, "fetched after the decision time", _iso(stamp)))
            continue
        approx = "" if best.get("source") == ASIA_EXACT_PARTIAL_SOURCE else f" (approximate source: {best.get('source')})"
        findings.append(Finding(f"asia_{code}", OK, False, f"{n}{approx}", _iso(stamp)))
        usable += 1

    findings.append(Finding(
        "asia_coverage", OK if usable >= MIN_ASIA_COVERAGE else MISSING, True,
        f"{usable}/{len(ASIA_INDEXES)} Asia indexes usable (need {MIN_ASIA_COVERAGE})",
    ))
    return findings


# ── Macro levels ──────────────────────────────────────────────────────────────

def check_macro(inputs: PremarketInputs) -> list[Finding]:
    t = inputs.prev_session
    macro = inputs.macro
    findings: list[Finding] = []
    frame = macro.copy() if macro is not None else pd.DataFrame()
    if not frame.empty:
        frame["_d"] = pd.to_datetime(frame["factor_date"])
        frame = frame.sort_values("_d")
    fields = [(f, True) for f in MACRO_REQUIRED] + [(f, False) for f in MACRO_OPTIONAL]
    for field, required in fields:
        if frame.empty or field not in frame.columns:
            findings.append(Finding(field, MISSING, required, "no MacroFactorDaily data"))
            continue
        today = frame[frame["_d"] == pd.Timestamp(t)]
        value = today[field].iloc[-1] if not today.empty else None
        if _isnull(value):
            findings.append(Finding(field, MISSING, required, f"NULL for {t}"))
            continue
        earlier = frame[(frame["_d"] < pd.Timestamp(t)) & frame[field].notna()]
        if not earlier.empty and float(earlier[field].iloc[-1]) == float(value):
            findings.append(Finding(field, STALE, required,
                                    f"identical to {earlier['_d'].iloc[-1].date()} value {value}"))
            continue
        findings.append(Finding(field, OK, required, f"{float(value):.4g}"))
    return findings


# ── Domestic features ─────────────────────────────────────────────────────────

def check_domestic(inputs: PremarketInputs) -> list[Finding]:
    t = inputs.prev_session
    row = inputs.features
    if row is None:
        return [Finding("signal_features", MISSING, True, f"no SignalFeatureDaily row for {t}")]
    findings = [Finding("signal_features", OK, True, f"row present for {t}")]
    for field in DOMESTIC_OPTIONAL:
        value = row.get(field)
        if _isnull(value):
            findings.append(Finding(field, MISSING, False, f"NULL for {t}"))
        else:
            findings.append(Finding(field, OK, False))
    volume = row.get("volume_day")
    if _isnull(volume) or float(volume) == 0.0:
        findings.append(Finding("volume_day", MISSING, False, f"volume is {volume!r} for {t} (OBV/volume features unreliable)"))
    else:
        findings.append(Finding("volume_day", OK, False))
    return findings


# ── Aggregate ─────────────────────────────────────────────────────────────────

def run_checks(inputs: PremarketInputs) -> list[Finding]:
    return [
        *check_gift(inputs),
        *check_western(inputs),
        *check_asia(inputs),
        *check_macro(inputs),
        *check_domestic(inputs),
    ]


def summarize(findings: list[Finding]) -> dict[str, Any]:
    """FAIL if a required field is not OK, WARN if only optional ones are not."""
    required_bad = [f for f in findings if f.required and not f.ok]
    optional_bad = [f for f in findings if not f.required and not f.ok]
    status = "FAIL" if required_bad else "WARN" if optional_bad else "OK"
    return {
        "status": status,
        "required_problems": [asdict(f) for f in required_bad],
        "optional_problems": [asdict(f) for f in optional_bad],
        "checked": len(findings),
    }
