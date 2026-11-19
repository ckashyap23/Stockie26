"""Step 3 - fetch the article body, only for deduplicated, relevant event clusters.

One `SiteFetcher` per publisher family, selected by domain. Every fetcher:
  - skips the network when the feed is marked `body: headline` (paywalled; judged on headline),
  - rate-limits per domain and caches by URL hash (reruns never refetch),
  - falls back to the RSS summary when the page is blocked, paywalled or too short, and records
    `content_source` so the judge/audit knows what it actually read.

Extraction order: JSON-LD `articleBody` (most news sites embed it) -> <p> text inside <article>
-> all <p> text. Rights: only enable body fetching for sources whose terms allow it
(see `body:` in data/feeds.yaml and the registry notes).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

from app.domain import RelevantArticle

log = logging.getLogger(__name__)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-IN,en;q=0.9",
}
_JSONLD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_BOILERPLATE = re.compile(
    r"(subscribe|sign in|log in|read more|also read|follow us|download the app|all rights reserved|"
    r"cookie|advertisement|copyright)", re.I,
)


class _ParagraphParser(HTMLParser):
    """Collects <p> text, remembering whether it sat inside <article>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_article = 0
        self.skip = 0
        self.in_p = False
        self.buf: list[str] = []
        self.article_paras: list[str] = []
        self.all_paras: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.in_article += 1
        elif tag in {"script", "style", "nav", "footer", "aside", "noscript", "form"}:
            self.skip += 1
        elif tag == "p" and not self.skip:
            self.in_p, self.buf = True, []

    def handle_endtag(self, tag):
        if tag == "article":
            self.in_article = max(0, self.in_article - 1)
        elif tag in {"script", "style", "nav", "footer", "aside", "noscript", "form"}:
            self.skip = max(0, self.skip - 1)
        elif tag == "p" and self.in_p:
            text = " ".join("".join(self.buf).split())
            if len(text) >= 40 and not _BOILERPLATE.search(text):
                self.all_paras.append(text)
                if self.in_article:
                    self.article_paras.append(text)
            self.in_p = False

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.buf.append(data)


def _walk_jsonld(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_jsonld(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_jsonld(value)


def extract_body(html: str) -> str:
    """Best-effort article body from raw HTML ('' when nothing usable)."""
    best = ""
    for block in _JSONLD.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in _walk_jsonld(data):
            body = node.get("articleBody")
            if isinstance(body, str) and len(body) > len(best):
                best = " ".join(body.split())
    if best:
        return best
    parser = _ParagraphParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must not fail the run
        pass
    paras = parser.article_paras or parser.all_paras
    return "\n".join(paras)


class SiteFetcher:
    """Generic fetcher; subclasses override `headers`, `clean` or `fetch_html` for site quirks."""

    domains: tuple[str, ...] = ()
    headers: dict[str, str] = {}
    min_chars: int | None = None  # shortest usable page for this publisher; None = the run-wide default

    def fetch_html(self, url: str, timeout: int) -> str:
        response = requests.get(url, headers={**HEADERS, **self.headers}, timeout=timeout)
        response.raise_for_status()
        return response.text

    def clean(self, body: str) -> str:
        return body

    def fetch(self, url: str, timeout: int) -> str:
        return self.clean(extract_body(self.fetch_html(url, timeout)))


class PublicRecordFetcher(SiteFetcher):
    """Government / central-bank pages (RBI, PIB, Fed): public, no paywall, no rights concern.

    A press release or statement is legitimately short (a few sentences), so the usual "under 400 characters means paywalled or blocked"
    rule would drop the primary source and leave the model a bare title. These pages count from 120 characters."""

    domains = ("rbi.org.in", "pib.gov.in", "federalreserve.gov")
    min_chars = 120


class TimesGroupFetcher(SiteFetcher):
    """Economic Times / Times Internet: drops the 'Also Read' / 'Follow us' tail lines."""

    domains = ("economictimes.indiatimes.com", "timesofindia.indiatimes.com")

    def clean(self, body: str) -> str:
        return "\n".join(line for line in body.splitlines() if not line.lower().startswith(("also read", "follow us")))


class HtMediaFetcher(SiteFetcher):
    """Mint / HT Media."""

    domains = ("livemint.com", "hindustantimes.com")


class IndianBusinessFetcher(SiteFetcher):
    """Moneycontrol, CNBC-TV18 / Network18, Business Today, BusinessLine."""

    domains = ("moneycontrol.com", "cnbctv18.com", "businesstoday.in", "thehindubusinessline.com")


class GlobalFreeFetcher(SiteFetcher):
    """BBC, CNBC, MarketWatch: free to read."""

    domains = ("bbc.com", "bbc.co.uk", "cnbc.com", "marketwatch.com")


_FETCHERS: tuple[SiteFetcher, ...] = (
    PublicRecordFetcher(), TimesGroupFetcher(), HtMediaFetcher(), IndianBusinessFetcher(), GlobalFreeFetcher(),
)
_GENERIC = SiteFetcher()


def fetcher_for(url: str) -> SiteFetcher:
    host = (urlparse(url).hostname or "").lower()
    for fetcher in _FETCHERS:
        if any(host == d or host.endswith("." + d) for d in fetcher.domains):
            return fetcher
    return _GENERIC


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".txt")


def extract_full_text(
    items: list[RelevantArticle],
    cache_dir: Path | None = None,
    max_fetches: int = 40,
    min_chars: int = 400,
    timeout: int = 20,
    domain_delay: float = 1.0,
    enabled: bool = True,
) -> tuple[list[RelevantArticle], str]:
    """Returns (items with content filled where possible, status string)."""
    if not enabled:
        return [_mark(i, "summary") for i in items], "disabled (judging on summaries)"
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    last_hit: dict[str, float] = {}
    done: dict[str, tuple[str, str]] = {}  # url -> (content, content_source)
    out: list[RelevantArticle] = []
    counts = {"full_text": 0, "headline_only": 0, "summary": 0, "cached": 0}
    fetches = 0

    for item in items:
        article = item.article
        if article.body_mode == "headline" or not article.url:
            out.append(_mark(item, "headline_only"))
            counts["headline_only"] += 1
            continue
        if article.url in done:
            content, source = done[article.url]
        else:
            content, source = "", "summary"
            cached = _cache_path(cache_dir, article.url) if cache_dir else None
            if cached and cached.exists():
                content, source = cached.read_text(encoding="utf-8"), "full_text"
                counts["cached"] += 1
            elif fetches < max_fetches:
                host = urlparse(article.url).hostname or ""
                wait = domain_delay - (time.monotonic() - last_hit.get(host, 0.0))
                if wait > 0:
                    time.sleep(wait)
                last_hit[host] = time.monotonic()
                fetches += 1
                fetcher = fetcher_for(article.url)
                try:
                    body = fetcher.fetch(article.url, timeout)
                except Exception as exc:  # noqa: BLE001 - blocked/paywalled pages fall back to the summary
                    log.info("body fetch failed for %s: %s", article.url, exc)
                    body = ""
                if len(body) >= (fetcher.min_chars if fetcher.min_chars is not None else min_chars):
                    content, source = body[:12000], "full_text"
                    if cached:
                        cached.write_text(content, encoding="utf-8")
            done[article.url] = (content, source)
        out.append(replace(item, article=replace(article, content=content, content_source=source)))
        counts[source if source in counts else "summary"] += 1

    status = (
        f"ok ({counts['full_text']} full text [{counts['cached']} cached], "
        f"{counts['headline_only']} headline-only, {counts['summary']} summary fallback; {fetches} fetches)"
    )
    return out, status


def _mark(item: RelevantArticle, source: str) -> RelevantArticle:
    return replace(item, article=replace(item.article, content_source=source))
