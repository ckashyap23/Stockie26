"""Step 2a - fetch headlines. RSS + NewsAPI fetchers transferred from src/news_sentiment/sources.py.

Changes from the original: no imports from `src.*`, feeds come from data/feeds.yaml,
article model is `app.domain.Article`, logging replaces print.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from app.domain import Article

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

NEWSAPI_QUERY = (
    "NIFTY OR Sensex OR India markets OR RBI OR rupee OR crude oil OR Fed OR inflation "
    "OR tariffs OR global markets OR US yields"
)


@dataclass(frozen=True)
class RssFeed:
    name: str
    url: str
    region: str = "india"
    fallback_urls: tuple[str, ...] = ()
    source_id: str = ""
    priority: str = "P2"
    body: str = "full"  # full | headline
    enabled: bool = True


def load_feeds(path: Path) -> list[RssFeed]:
    """Flatten data/feeds.yaml (publishers -> feeds), skipping disabled publishers/feeds."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feeds: list[RssFeed] = []
    for pub in raw.get("publishers", []):
        if not pub.get("enabled", True):
            continue
        region = "india" if str(pub.get("geography", "")).lower().startswith("domestic") else "global"
        for item in pub.get("feeds", []):
            if not item.get("enabled", True):
                continue
            feeds.append(RssFeed(
                name=str(item["name"]),
                url=str(item["url"]),
                region=region,
                fallback_urls=tuple(item.get("fallback_urls") or ()),
                source_id=str(pub["id"]),
                priority=str(pub.get("priority", "P2")),
                body=str(pub.get("body", "full")),
            ))
    return feeds


def fetch_headlines(
    feeds: list[RssFeed],
    window_start: datetime,
    window_end: datetime,
    newsapi_key: str = "",
    timeout_seconds: int = 20,
) -> list[Article]:
    fetched_at = datetime.now(IST)
    articles: list[Article] = []
    # One bad / slow feed must not stall the run: fetch feeds concurrently.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(feeds)))) as pool:
        for batch in pool.map(
            lambda feed: _fetch_rss_feed(feed, window_start, window_end, fetched_at, timeout_seconds), feeds
        ):
            articles.extend(batch)
    if newsapi_key:
        articles.extend(_fetch_newsapi(newsapi_key, window_start, window_end, fetched_at, timeout_seconds))
    return _dedupe(articles)


def _fetch_rss_feed(feed, window_start, window_end, fetched_at, timeout_seconds) -> list[Article]:
    for url in (feed.url, *feed.fallback_urls):
        articles = _fetch_rss_url(feed, url, window_start, window_end, fetched_at, timeout_seconds)
        if articles:
            return articles
    return []


def _fetch_rss_url(feed, url, window_start, window_end, fetched_at, timeout_seconds) -> list[Article]:
    try:
        response = requests.get(url, timeout=timeout_seconds, headers=RSS_HEADERS)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - one feed must not fail the run.
        log.warning("RSS fetch failed for %s (%s): %s", feed.name, url, exc)
        return []

    text = response.content[:200].decode("utf-8-sig", errors="replace").lstrip()  # RBI/PIB/Fed: BOM, no charset header
    if not (text.startswith("<?xml") or text.startswith("<rss") or text.startswith("<feed")):
        log.warning("RSS response for %s was not XML; skipped %s", feed.name, url)
        return []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        log.warning("RSS parse failed for %s (%s): %s", feed.name, url, exc)
        return []

    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    articles: list[Article] = []
    for item in items:
        title = _node_text(item, "title")
        link = _node_text(item, "link") or _node_text(item, "guid")
        summary = _node_text(item, "description") or _node_text(item, "summary")
        published_raw = _node_text(item, "pubDate") or _node_text(item, "published") or _node_text(item, "updated")
        published_at = parse_datetime(published_raw) or fetched_at
        if published_at.astimezone(IST) < window_start:
            continue  # the upper bound (cutoff) is applied on first_published after the archive merge
        articles.append(_article(
            feed.name, link, title, summary, published_at, fetched_at, feed.region, "rss",
            source_id=feed.source_id, body_mode=feed.body, priority=feed.priority,
        ))
    return articles


def _fetch_newsapi(api_key, window_start, window_end, fetched_at, timeout_seconds) -> list[Article]:
    params = {
        "q": NEWSAPI_QUERY,
        "language": "en",
        "sortBy": "publishedAt",
        "from": window_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "to": window_end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "pageSize": 100,
        "apiKey": api_key,
    }
    try:
        response = requests.get("https://newsapi.org/v2/everything", params=params, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("NewsAPI fetch failed: %s: %s", type(exc).__name__, exc)
        return []

    articles: list[Article] = []
    for item in payload.get("articles") or []:
        published_at = parse_datetime(str(item.get("publishedAt") or "")) or fetched_at
        if not _in_window(published_at, window_start, window_end):
            continue
        source = (item.get("source") or {}).get("name") or "NewsAPI"
        articles.append(_article(
            source, str(item.get("url") or ""), str(item.get("title") or ""),
            str(item.get("description") or item.get("content") or ""),
            published_at, fetched_at, "global", "newsapi",
        ))
    return articles


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
    except Exception:  # noqa: BLE001
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _article(
    source, url, title, summary, published_at, fetched_at, region, provider,
    source_id: str = "", body_mode: str = "full", priority: str = "P2",
) -> Article:
    clean_title, clean_summary, clean_url = _clean_text(title), _clean_text(summary), url.strip()
    # The id must not depend on the publish time: publishers revise pubDate, and a revision is the
    # same article (tracked as `updated_at` by the archive), not a new one.
    article_id = hashlib.sha256((clean_url or f"{source}|{clean_title}").encode("utf-8")).hexdigest()[:16]
    return Article(
        article_id, source, clean_url, clean_title, clean_summary, published_at, fetched_at, region, provider,
        source_id=source_id, body_mode=body_mode, source_priority=priority,
    )


def _node_text(item: ET.Element, tag: str) -> str:
    found = item.find(tag)
    if found is not None and found.text:
        return found.text
    found = item.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
    return found.text if found is not None and found.text else ""


def _clean_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _in_window(timestamp: datetime, window_start: datetime, window_end: datetime) -> bool:
    return window_start <= timestamp.astimezone(IST) <= window_end


def _dedupe(articles: list[Article]) -> list[Article]:
    by_id = {article.article_id: article for article in articles}
    return sorted(by_id.values(), key=lambda item: (item.published_at, item.source, item.title))
