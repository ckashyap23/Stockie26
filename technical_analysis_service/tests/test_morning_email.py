from datetime import date

import pytest

from technical_analysis_service.common import mailer
from technical_analysis_service.railway_scheduler import due_jobs, schedule
from technical_analysis_service.scheduled_jobs import JOBS
from technical_analysis_service.scripts.daily_NIFTY import daily_morning_email as morning

D = date(2026, 11, 17)
SUMMARY = {
    "signal_date": date(2026, 11, 16), "direction": "PUT", "base_prediction": "PUT", "strength_score": 3.2,
    "selected_strategy": "TrendContinuationPut", "option_instrument": "NIFTY26N2422900PE", "strike": 22900,
    "expiry": date(2026, 11, 24), "option_type": "PE", "entry_ref_price": 410.8, "no_trade_reason": None,
    "news_score": -0.14, "news_direction": "negative", "news_confidence": 0.62,
}
TRADE = {
    "option_symbol": "NIFTY26N2422900PE", "quantity": 260, "lot_size": 65, "planned_entry_price": 410.8, "entry_price": 411.0,
    "target_1_price": 540.6, "target_1_pct": 0.006, "stop_loss_price": 400.9, "stop_loss_pct": 0.004,
    "signal_status": "PLANNED", "trade_status": "OPEN",
}


def test_subject_has_trade_date_prediction_and_news_score():
    subject, text, html = morning.build_message(D, "NIFTY", SUMMARY, [TRADE], "Attached: x.csv")
    assert subject == "Stockie 2026-11-17 | NIFTY PUT | News -0.14"
    assert "<pre" in html and "Attached: x.csv" in text


def test_body_carries_option_and_paper_trade_details():
    _, text, _ = morning.build_message(D, "NIFTY", SUMMARY, [TRADE], "")
    assert "NIFTY26N2422900PE" in text and "4 lots x 65 = 260 units" in text
    assert "Rs 106,860.00" in text  # 260 x the actual entry price 411.0
    assert "Target: 540.60  (0.60% NIFTY move)" in text and "Stop loss: 400.90  (0.40% NIFTY move)" in text


def test_trade_without_paper_row_and_no_trade_and_missing_news_are_stated_not_fatal():
    _, text, _ = morning.build_message(D, "NIFTY", SUMMARY, [], "")
    assert "Paper trade: none found yet" in text
    subject, text, _ = morning.build_message(D, "NIFTY", {**SUMMARY, "direction": "NO_POSITION", "news_score": None, "no_trade_reason": "low strength"}, [], "")
    assert subject == "Stockie 2026-11-17 | NIFTY NO TRADE | News n/a"
    assert "No option trade for this date. Reason: low strength" in text and "has not published a score" in text
    subject, text, _ = morning.build_message(D, "NIFTY", {}, [], "")
    assert "NO PREDICTION" in subject and "No prediction row" in text


def test_news_attachment_failures_become_a_note(monkeypatch):
    monkeypatch.delenv("NEWS_SERVICE_URL", raising=False)
    assert morning.fetch_news_csv(D)[0] is None
    monkeypatch.setenv("NEWS_SERVICE_URL", "http://news.internal:8080/")
    monkeypatch.setenv("NEWS_API_KEY", "k")

    class Response:
        status_code = 404
        content = b""

    monkeypatch.setattr(morning.requests, "get", lambda url, **kw: Response())
    data, note = morning.fetch_news_csv(D)
    assert data is None and "no stored signal" in note

    Response.status_code, Response.content = 200, b"\xef\xbb\xbfa,b\r\n1,2\r\n3,4\r\n"
    seen = {}
    monkeypatch.setattr(morning.requests, "get", lambda url, **kw: seen.update(url=url, headers=kw["headers"]) or Response())
    data, note = morning.fetch_news_csv(D)
    assert data and "2 rows" in note
    assert seen["url"] == "http://news.internal:8080/v1/signal/2026-11-17/csv" and seen["headers"] == {"X-API-Key": "k"}


def test_resend_payload_and_misconfiguration(monkeypatch):
    for name in ("EMAIL_PROVIDER", "RESEND_API_KEY", "EMAIL_FROM", "NOTIFY_EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        mailer.send_email("s", "t")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_key")
    monkeypatch.setenv("EMAIL_FROM", "Stockie <a@b.com>")
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "x@y.com, z@y.com")
    sent = {}

    class Ok:
        status_code = 200

        def json(self):
            return {"id": "msg-1"}

    monkeypatch.setattr(mailer.requests, "post", lambda url, json, headers, timeout: sent.update(url=url, json=json, headers=headers) or Ok())
    assert mailer.send_email("subj", "body", "<b>x</b>", [("a.csv", b"abc")]) == "msg-1"
    assert sent["url"] == mailer.API_URL and sent["headers"]["Authorization"] == "Bearer re_key"
    assert sent["json"]["to"] == ["x@y.com", "z@y.com"] and sent["json"]["attachments"] == [{"filename": "a.csv", "content": "YWJj"}]


def test_job_is_on_the_roster_at_0932_and_scheduler_fires_it():
    assert JOBS["morning-email"][3:] == ("09:32", "11:00", [])
    assert "09:32" in schedule()["morning-email"]
    from datetime import datetime
    from zoneinfo import ZoneInfo

    assert "morning-email" in due_jobs(datetime(2026, 11, 17, 9, 32, tzinfo=ZoneInfo("Asia/Kolkata")))
