"""Drop broker calls and stock-pick lists before seed matching (rules: data/commentary_filters.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.domain import Article


@dataclass(frozen=True)
class CommentaryHit:
    category: str
    pattern: str


class CommentaryFilter:
    def __init__(self, categories: dict[str, list[str]]) -> None:
        self._rules = [
            (category, pattern, re.compile(pattern, re.IGNORECASE))
            for category, patterns in categories.items()
            for pattern in patterns
        ]

    @classmethod
    def load(cls, path: Path) -> "CommentaryFilter":
        if not path.exists():
            return cls({})
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls({str(k): [str(p) for p in v or []] for k, v in (raw.get("categories") or {}).items()})

    def classify(self, article: Article) -> CommentaryHit | None:
        """Headline-only test: a macro forecast mentioned in a summary must not trigger a drop."""
        for category, pattern, regex in self._rules:
            if regex.search(article.title):
                return CommentaryHit(category, pattern)
        return None

    def split(self, articles: list[Article]) -> tuple[list[Article], list[tuple[Article, CommentaryHit]]]:
        kept: list[Article] = []
        dropped: list[tuple[Article, CommentaryHit]] = []
        for article in articles:
            hit = self.classify(article)
            if hit:
                dropped.append((article, hit))
            else:
                kept.append(article)
        return kept, dropped
