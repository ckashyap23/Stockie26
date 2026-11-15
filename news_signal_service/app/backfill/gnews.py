"""Historical headlines from Google News search (date-filtered RSS), for backfills.

Limits, by design of the source (verified 2026-11-09):
  - For dates more than a few days old the feed gives only the DATE of each item (every pubDate is 07:00 UTC), not the time.
  - Each query returns only its most relevant ~15-60 items for the range, so coverage is partial.
  - Links are Google redirect URLs: article bodies cannot be fetched, so backfilled events are judged on the headline alone.
Terms of use of Google News are unreviewed (enabled by the owner for live runs; the same decision is applied here).

To make the existing point-in-time replay logic apply unchanged, an item dated X (IST) is stored with the synthetic first-published and
first-ingested time `X 16:00 IST`. For target day D (window = previous weekday 15:30 -> D 08:30) that selects exactly the items dated
from the previous weekday through the day before D, and never anything dated D itself (no look-ahead).
"""
from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests
import yaml

from app.domain import Article
from app.pipeline.ingest import _article

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
SYNTHETIC_TIME = dtime(16, 0)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
URL = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


@dataclass(frozen=True)
class Hit:
    day: date  # IST calendar day of the item
    title: str
    link: str
    source: str


class _Pacer:
    """Global request spacing shared by all threads."""

    def __init__(self, min_interval: float) -> None:
        self._interval, self._lock, self._next = min_interval, threading.Lock(), 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            time.sleep(delay)


def load_queries(path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(q) for q in raw.get("queries", [])]


def search(query: str, after: date, before: date, pacer: _Pacer | None = None, get=requests.get, sleep=time.sleep) -> list[Hit]:
    """One date-filtered Google News query. after/before are dates; callers filter the returned items by `Hit.day`."""
    url = URL.format(q=requests.utils.quote(f"{query} after:{after.isoformat()} before:{before.isoformat()}"))
    for attempt in range(4):
        if pacer:
            pacer.wait()
        try:
            response = get(url, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            log.warning("gnews network error (%s)", type(exc).__name__)
            sleep(2 ** attempt)
            continue
        if response.status_code in (429, 503):
            sleep(5 * (attempt + 1))
            continue
        if response.status_code != 200:
            log.warning("gnews HTTP %s for %r", response.status_code, query)
            return []
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            return []
        hits = []
        for item in root.findall(".//item"):
            raw_date = item.findtext("pubDate")
            if not raw_date or not item.findtext("link"):
                continue
            stamp = parsedate_to_datetime(raw_date).astimezone(IST)
            source = (item.findtext("source") or "").strip() or "Google News"
            hits.append(Hit(stamp.date(), (item.findtext("title") or "").strip(), item.findtext("link").strip(), source))
        return hits
    log.warning("gnews gave up on %r after retries", query)
    return []


def window_days(target: date) -> tuple[date, date]:
    """Calendar days whose items belong to `target`: previous weekday .. the day before target (inclusive)."""
    prev = target - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev, target - timedelta(days=1)


def to_article(hit: Hit) -> Article:
    stamp = datetime.combine(hit.day, SYNTHETIC_TIME, tzinfo=IST)
    return _article(hit.source, hit.link, hit.title, "", stamp, stamp, "global", "backfill_gnews",
                    source_id="S11", body_mode="headline", priority="P1")


def fetch_target(target: date, queries: list[str], pacer: _Pacer | None = None, get=requests.get) -> list[Article]:
    """All headlines belonging to `target`'s window, de-duplicated by link."""
    first, last = window_days(target)
    seen: dict[str, Hit] = {}
    for query in queries:
        for hit in search(query, first, target, pacer, get=get):
            if first <= hit.day <= last:
                seen.setdefault(hit.link, hit)
    return [to_article(h) for h in seen.values()]
