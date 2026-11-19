"""Pre-LLM non-event filter (data/commentary_filters.yaml: non_event + url_categories) and short primary-source pages."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import SERVICE_ROOT
from app.domain import Article, RelevantArticle
from app.pipeline.commentary import CommentaryFilter
from app.pipeline.extract import extract_full_text, fetcher_for

T0 = datetime(2026, 11, 19, 7, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
FILTER = CommentaryFilter.load(SERVICE_ROOT / "data" / "commentary_filters.yaml")


def _article(title, url="http://example.com/a", **kw):
    return Article("a1", "Src", url, title, "", T0, T0, "india", "rss", **kw)


@pytest.mark.parametrize("title", [
    "Stock Market LIVE Updates: Sensex, Nifty open higher",
    "Sensex Today | Stock market live: Nifty near 25,000",
    "Markets wrap: Sensex ends flat as IT drags",
    "Market Pulse: what's driving the Nifty",
    "Stocks to watch today: Infosys, HDFC Bank",
    "Key triggers for the market this week",
    "What to expect from Nifty on Monday",
    "Outlook today: Nifty may open gap-up",
    "Nifty prediction for tomorrow",
    "Latest Nifty news and analysis",
    "What's likely to move the market on Thursday",
    "Thursday's big stock stories",
    "Should you buy Reliance ahead of results?",
    "Here's what analysts are saying about Nvidia",
    "Read the full interview with the RBI governor",
    "Transcript: Fed chair press conference",
])
def test_wraps_previews_roundups_and_interviews_are_dropped_before_the_llm(title):
    hit = FILTER.classify(_article(title))
    assert hit is not None and hit.category in {"non_event", "stock_pick", "broker_call", "not_a_fresh_event"}, title


@pytest.mark.parametrize("url", [
    "https://www.bloomberg.com/news/videos/2026-11-19/markets-daily",
    "https://www.bloomberg.com/news/newsletters/2026-11-19/five-things",
    "https://www.cnbctv18.com/photos/markets/diwali-muhurat-trading-in-pictures-19999.htm",
])
def test_video_newsletter_and_photo_pages_are_dropped_by_url(url):
    hit = FILTER.classify(_article("A harmless headline", url=url))
    assert hit is not None and hit.category == "non_event_url"


@pytest.mark.parametrize("title", [
    "Fed minutes leave door open to larger December rate hike",
    "Dow Jones falls 550 points",
    "Brent crude settles above $92 a barrel",
    "RBI raises repo rate by 25 bps to 6.75%",
    "India's retail inflation rises to 5.5% in October",
    "Infosys hit with DOJ probe over H-1B filings",
])
def test_real_events_are_not_dropped(title):
    assert FILTER.classify(_article(title, url="https://www.reuters.com/markets/story-1")) is None, title


def test_a_summary_mentioning_a_wrap_phrase_does_not_drop_the_item():
    article = Article("a1", "Src", "http://example.com/a", "RBI raises repo rate by 25 bps", "markets wrap: prediction: what to expect",
                      T0, T0, "india", "rss")
    assert FILTER.classify(article) is None  # headline and URL only


def test_public_record_pages_count_from_120_characters_but_news_sites_still_need_400(tmp_path, monkeypatch):
    short_page = "<html><body><article><p>" + "The Board released the minutes of the Committee meeting held on October 28-29. " * 2 + "</p></article></body></html>"

    class Resp:
        text = short_page

        def raise_for_status(self):
            pass

    monkeypatch.setattr("app.pipeline.extract.requests.get", lambda url, **kw: Resp())
    fed = Article("f", "Federal Reserve", "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261119a.htm",
                  "Minutes of the FOMC", "", T0, T0, "intl", "rss")
    news = Article("n", "Some Paper", "https://www.example-news.com/story", "Minutes of the FOMC", "", T0, T0, "intl", "rss")
    items = [RelevantArticle(a, "M04", "fam", "INTL", 1.0, ()) for a in (fed, news)]
    out, _ = extract_full_text(items, cache_dir=tmp_path, domain_delay=0)
    assert [o.article.content_source for o in out] == ["full_text", "summary"]
    assert fetcher_for(fed.url).min_chars == 120 and fetcher_for(news.url).min_chars is None
