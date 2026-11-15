from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from technical_analysis_service.data_manager.data_quality import (
    LATE,
    MISSING,
    NOT_FINAL,
    OK,
    STALE,
    PremarketInputs,
    check_asia,
    check_domestic,
    check_gift,
    check_macro,
    check_western,
    previous_weekday,
    run_checks,
    summarize,
)

IST = ZoneInfo("Asia/Kolkata")
N = date(2026, 10, 28)       # decision day (Wednesday)
T = date(2026, 10, 27)       # previous Indian session
GLOBAL_COLUMNS = ["index_code", "trade_date", "open_price", "close_price", "source", "is_final", "fetched_at"]


def _global(rows):
    return pd.DataFrame(rows, columns=GLOBAL_COLUMNS)


def _western(code, day, open_, close, fetched, final=True):
    return (code, day, open_, close, "yfinance_1d", final, fetched)


def _inputs(**overrides):
    base = dict(
        decision_date=N,
        prev_session=T,
        now=None,
        gift={},
        global_rows=_global([]),
        macro=pd.DataFrame(),
        features=None,
    )
    base.update(overrides)
    return PremarketInputs(**base)


def _status(findings, field):
    return next(f.status for f in findings if f.field == field)


def test_previous_weekday_skips_weekend():
    assert previous_weekday(date(2026, 10, 26)) == date(2026, 10, 23)   # Monday -> Friday
    assert previous_weekday(N) == date(2026, 10, 27)


# ── GIFT ──────────────────────────────────────────────────────────────────────

def test_gift_ok_missing_prev_close_and_stale():
    fetched = datetime(2026, 10, 28, 9, 11, tzinfo=IST).replace(tzinfo=None)
    ok = check_gift(_inputs(gift={
        N: {"gift_0910": 22633.5, "fetched_at": fetched},
        T: {"gift_0910": 22500.0, "gift_1515": 22580.0},
    }))
    assert _status(ok, "gift_0910") == OK
    assert _status(ok, "gift_1515_prev") == OK

    missing = check_gift(_inputs(gift={N: {"gift_0910": 22633.5, "fetched_at": fetched}, T: {}}))
    assert _status(missing, "gift_1515_prev") == MISSING   # never filled with a fallback

    stale = check_gift(_inputs(gift={
        N: {"gift_0910": 22500.0, "fetched_at": fetched},
        T: {"gift_0910": 22500.0, "gift_1515": 22580.0},
    }))
    assert _status(stale, "gift_0910") == STALE

    absent = check_gift(_inputs(gift={N: {}, T: {"gift_1515": 22580.0}}))
    assert _status(absent, "gift_0910") == MISSING


def test_gift_fetched_after_decision_time_is_late_only_on_live_runs():
    late_fetch = datetime(2026, 10, 28, 9, 25)
    gift = {N: {"gift_0910": 22633.5, "fetched_at": late_fetch}, T: {"gift_1515": 22580.0}}
    live = check_gift(_inputs(gift=gift, now=datetime(2026, 10, 28, 9, 30, tzinfo=IST)))
    replay = check_gift(_inputs(gift=gift, now=None))
    assert _status(live, "gift_0910") == LATE
    assert _status(replay, "gift_0910") == OK


# ── US / Europe ───────────────────────────────────────────────────────────────

def _full_session(day, fetched, scale=1.0):
    codes = ["SP500", "NASDAQ", "DOW", "RUSSELL2000", "FTSE100", "DAX", "CAC40"]
    return [_western(c, day, 100.0 * scale, 101.0 * scale, fetched) for c in codes]


def test_western_ok_and_all_rows_missing():
    after_close = datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)
    rows = _full_session(T, after_close) + _full_session(date(2026, 10, 26), after_close, scale=1.1)
    ok = check_western(_inputs(global_rows=_global(rows)))
    assert all(f.status == OK for f in ok)

    none = check_western(_inputs(global_rows=_global(_full_session(date(2026, 10, 22), after_close))))
    assert [f.status for f in none] == [MISSING]
    assert none[0].required


def test_western_row_fetched_before_session_close_is_not_final():
    # The DAX 19 Oct bug: an intraday value was stored as a final close.
    midday = datetime(2026, 10, 27, 10, 31, tzinfo=timezone.utc)
    rows = _full_session(T, datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc))
    rows = [r for r in rows if r[0] != "DAX"] + [_western("DAX", T, 100.0, 101.0, midday)]
    result = check_western(_inputs(global_rows=_global(rows)))
    assert _status(result, "ohlc_DAX") == NOT_FINAL
    assert _status(result, "ohlc_SP500") == OK


def test_western_identical_to_previous_session_is_stale_and_required_for_sp500():
    after = datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)
    prior = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
    rows = _full_session(T, after) + _full_session(date(2026, 10, 26), prior)   # same prices both days
    result = check_western(_inputs(global_rows=_global(rows)))
    assert _status(result, "ohlc_SP500") == STALE
    assert next(f for f in result if f.field == "ohlc_SP500").required
    assert not next(f for f in result if f.field == "ohlc_DAX").required


# ── Asia ──────────────────────────────────────────────────────────────────────

def _asia_rows(codes, include_partial=True):
    rows = []
    for code in codes:
        rows.append((code, T, 100.0, 101.0, "yfinance_1d", True, datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)))
        if include_partial:
            rows.append((code, N, 101.0, 102.5, "yfinance_5m_partial_0900", False,
                         datetime(2026, 10, 28, 3, 36, tzinfo=timezone.utc)))
    return rows


def test_asia_coverage_threshold():
    five = ["NIKKEI225", "HANG_SENG", "SHANGHAI", "KOSPI", "ASX200"]
    enough = check_asia(_inputs(global_rows=_global(_asia_rows(five[:3]))))
    assert _status(enough, "asia_coverage") == OK
    assert _status(enough, "asia_HANG_SENG") == OK
    assert _status(enough, "asia_KOSPI") == MISSING          # optional on its own

    too_few = check_asia(_inputs(global_rows=_global(_asia_rows(five[:2]))))
    assert _status(too_few, "asia_coverage") == MISSING
    assert next(f for f in too_few if f.field == "asia_coverage").required


# ── Macro ─────────────────────────────────────────────────────────────────────

def test_macro_missing_stale_and_ok():
    macro = pd.DataFrame([
        {"factor_date": date(2026, 10, 26), "brent_close": 115.8, "us10y_level": 4.986, "usdinr_close": 94.9,
         "wti_close": 101.7, "india_vix": 15.0, "fii_net_cr": None, "dii_net_cr": None},
        {"factor_date": T, "brent_close": 113.9, "us10y_level": 4.986, "usdinr_close": None,
         "wti_close": 99.9, "india_vix": None, "fii_net_cr": None, "dii_net_cr": None},
    ])
    result = check_macro(_inputs(macro=macro))
    assert _status(result, "brent_close") == OK
    assert _status(result, "us10y_level") == STALE          # identical to the prior session
    assert _status(result, "usdinr_close") == MISSING       # NULL is never zero-filled
    assert _status(result, "india_vix") == MISSING
    assert not next(f for f in result if f.field == "india_vix").required


# ── Domestic ──────────────────────────────────────────────────────────────────

def test_domestic_flags_null_fields_and_zero_volume():
    assert check_domestic(_inputs(features=None))[0].status == MISSING
    row = {"pcr_oi": None, "oi_regime": "LONG_BUILDUP", "futures_oi": 1.0, "breadth_advance_decline_ratio": 0.5,
           "breadth_new_high_ratio": 0.1, "banknifty_rel_strength": 0.2, "volume_day": 0}
    result = check_domestic(_inputs(features=row))
    assert _status(result, "pcr_oi") == MISSING
    assert _status(result, "oi_regime") == OK
    assert _status(result, "volume_day") == MISSING


# ── Summary ───────────────────────────────────────────────────────────────────

def test_summary_distinguishes_fail_warn_ok():
    fine = datetime(2026, 10, 28, 9, 11)
    full = _inputs(
        gift={N: {"gift_0910": 22633.5, "fetched_at": fine}, T: {"gift_1515": 22580.0}},
        global_rows=_global(
            _full_session(T, datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc))
            + _full_session(date(2026, 10, 26), datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc), scale=1.1)
            + _asia_rows(["NIKKEI225", "HANG_SENG", "SHANGHAI", "KOSPI", "ASX200"])
        ),
        macro=pd.DataFrame([
            {"factor_date": date(2026, 10, 26), "brent_close": 115.8, "us10y_level": 4.986, "usdinr_close": 94.9,
             "wti_close": 101.7, "india_vix": 15.0, "fii_net_cr": 1.0, "dii_net_cr": 1.0},
            {"factor_date": T, "brent_close": 113.9, "us10y_level": 4.993, "usdinr_close": 95.2,
             "wti_close": 99.9, "india_vix": 15.5, "fii_net_cr": 2.0, "dii_net_cr": 2.0},
        ]),
        features={"pcr_oi": 1.0, "oi_regime": "X", "futures_oi": 1.0, "breadth_advance_decline_ratio": 1.0,
                  "breadth_new_high_ratio": 0.1, "banknifty_rel_strength": 0.1, "volume_day": 100},
    )
    assert summarize(run_checks(full))["status"] == OK

    warn = summarize(run_checks(_inputs(**{**full.__dict__, "features": {**full.features, "pcr_oi": None}})))
    assert warn["status"] == "WARN"

    fail = summarize(run_checks(_inputs(**{**full.__dict__, "gift": {N: full.gift[N], T: {}}})))
    assert fail["status"] == "FAIL"
    assert fail["required_problems"][0]["field"] == "gift_1515_prev"
