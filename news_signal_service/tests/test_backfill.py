import csv
import json
from datetime import date
from zoneinfo import ZoneInfo

from app.backfill import gnews, run
from app.config import Settings
from app.pipeline.orchestrator import news_window
from app.storage.archive import ArticleArchive

IST = ZoneInfo("Asia/Kolkata")


def _rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><pubDate>{d} 07:00:00 GMT</pubDate><source>{s}</source></item>"
        for t, l, d, s in items
    )
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


class FakeGet:
    def __init__(self, items):
        self.items, self.calls = items, []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append(url)
        content = _rss(self.items)

        class R:
            status_code = 200

        R.content = content
        return R()


MON, TUE, FRI, SAT, SUN = "Mon, 05 Oct 2026", "Tue, 06 Oct 2026", "Fri, 02 Oct 2026", "Sat, 03 Oct 2026", "Sun, 04 Oct 2026"


def test_window_days_follow_the_previous_weekday_rule():
    assert gnews.window_days(date(2026, 10, 6)) == (date(2026, 10, 5), date(2026, 10, 5))  # Tue: Monday only
    assert gnews.window_days(date(2026, 10, 5)) == (date(2026, 10, 2), date(2026, 10, 4))  # Mon: Fri, Sat, Sun


def test_fetch_keeps_only_window_dates_dedupes_by_link_and_stamps_a_synthetic_time():
    items = [("Oil jumps", "http://g/1", MON, "Reuters"), ("Oil jumps again", "http://g/1", MON, "Reuters"),
             ("Fed holds", "http://g/2", MON, "CNBC"), ("Dated target day", "http://g/3", TUE, "ET"),
             ("Too early", "http://g/4", FRI, "ET")]
    get = FakeGet(items)
    articles = gnews.fetch_target(date(2026, 10, 6), ["q1", "q2"], get=get)
    assert sorted(a.url for a in articles) == ["http://g/1", "http://g/2"]  # duplicate link collapsed; Tue and Fri excluded
    a = articles[0]
    assert (a.published_at.hour, a.published_at.minute) == (16, 0) and a.published_at.date() == date(2026, 10, 5)
    assert a.provider == "backfill_gnews" and a.body_mode == "headline" and a.summary == ""
    assert len(get.calls) == 2 and "after%3A2026-10-05" in get.calls[0]


def test_replay_window_selects_exactly_the_right_days_and_nothing_dated_the_target_day(tmp_path):
    archive = ArticleArchive(tmp_path / "a.sqlite3")
    items = [("Fri news", "http://g/f", FRI, "X"), ("Sat news", "http://g/s", SAT, "X"), ("Sun news", "http://g/u", SUN, "X"),
             ("Mon news", "http://g/m", MON, "X"), ("Tue news", "http://g/t", TUE, "X")]
    get = FakeGet(items)
    for target in (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)):
        archive.record(gnews.fetch_target(target, ["q"], get=get))
    settings = Settings(data_dir=tmp_path)

    def titles(target):
        start, cutoff = news_window(target, settings)
        return sorted(a.title for a in archive.as_of(start, cutoff, settings.ingest_grace_minutes))

    assert titles(date(2026, 10, 5)) == ["Fri news", "Sat news", "Sun news"]  # Monday gets the weekend, never Monday's own news
    assert titles(date(2026, 10, 6)) == ["Mon news"]
    assert titles(date(2026, 10, 7)) == ["Tue news"]


def _result(score=0.2, fetched=10, unscored=0, errors=None):
    return {"score": score, "direction": "positive", "magnitude": abs(score), "confidence": 0.5, "article_count_fetched": fetched,
            "article_count_relevant": 4, "groups": [{"group": "OIL_GEO", "score": -0.4}], "run_id": "d/1",
            "evidence": [{"signal": -0.3, "headline": "Oil jumps"}],
            "judge_summary": {"rules": 3, "llm": 2, "llm_judged": 2 - unscored, "llm_vetoed": 1, "llm_unscored": unscored,
                              "llm_calls": 1, "llm_errors": errors or []}}


def test_row_status_complete_partial_no_news():
    d = date(2026, 10, 6)
    assert run.row_from_result(d, _result())["status"] == "complete"
    assert run.row_from_result(d, _result(unscored=1))["status"] == "partial"
    assert run.row_from_result(d, _result(fetched=0))["status"] == "no_news"
    row = run.row_from_result(d, _result())
    assert row["weekday"] == "Tue" and row["top_groups"] == "OIL_GEO:-0.40" and row["top_headline"] == "Oil jumps"


def test_weekdays_skip_weekends():
    assert [d.day for d in run.weekdays(date(2026, 10, 2), date(2026, 10, 7))] == [2, 5, 6, 7]


def _drive(monkeypatch, results):
    """Replace run_signal with scripted results (one per call); returns the list of recorded calls."""
    calls = []

    class Fake:
        def __init__(self, payload):
            self.payload = payload

        def to_dict(self):
            return self.payload

    def fake_run_signal(target, settings, replay=False, record=False):
        calls.append((target, replay, record))
        return Fake({**results.pop(0), "target_date": target.isoformat()})

    monkeypatch.setattr(run, "run_signal", fake_run_signal)
    return calls


def test_judge_phase_writes_rows_csv_and_resumes_only_incomplete_dates(monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path)
    state, state_path, csv_path = {"fetched": {}, "rows": {}}, tmp_path / "s.json", tmp_path / "scores.csv"
    dates = [date(2026, 10, 5), date(2026, 10, 6)]
    calls = _drive(monkeypatch, [_result(0.1), _result(0.2, unscored=1, errors=["boom"])])
    assert run.judge_phase(dates, settings, state, state_path, csv_path, resume=False) == 0
    assert all(replay and record for _, replay, record in calls)  # always a recorded replay
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert [r["date"] for r in rows] == ["2026-10-05", "2026-10-06"] and [r["status"] for r in rows] == ["complete", "partial"]
    assert (tmp_path / "signals" / "2026-10-05.json").exists()

    calls2 = _drive(monkeypatch, [_result(0.25)])
    run.judge_phase(dates, settings, state, state_path, csv_path, resume=True)
    assert [c[0] for c in calls2] == [date(2026, 10, 6)]  # only the partial date is redone
    assert json.loads(state_path.read_text())["rows"]["2026-10-06"]["status"] == "complete"


def test_judge_phase_stops_when_the_llm_quota_is_exhausted(monkeypatch, tmp_path):
    quota = ["groq: rate limited / quota exhausted (429) after retries"]
    dates = [date(2026, 10, d) for d in (5, 6, 7, 8)]
    calls = _drive(monkeypatch, [_result(unscored=2, errors=quota) for _ in dates])
    code = run.judge_phase(dates, Settings(data_dir=tmp_path), {"fetched": {}, "rows": {}}, tmp_path / "s.json", tmp_path / "c.csv", False)
    assert code == 3 and len(calls) == 2  # stopped after two consecutive quota failures


def test_fetch_phase_skips_dates_already_fetched(tmp_path):
    settings = Settings(data_dir=tmp_path)
    state = {"fetched": {"2026-10-06": 1}, "rows": {}}
    get = FakeGet([("Mon news", "http://g/m", MON, "X")])
    run.fetch_phase([date(2026, 10, 6), date(2026, 10, 7)], settings, state, tmp_path / "s.json", ["q"], workers=1, interval=0.0, get=get)
    assert "2026-10-07" in state["fetched"] and len(get.calls) == 1  # only the unfetched date was requested
