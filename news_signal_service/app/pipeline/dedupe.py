"""Step 2a - group headlines that describe the same event (before any seed matching).

Single-link clustering on embedding cosine, with a title-token Jaccard as a second route so an exact syndicated copy is always caught.
Each cluster keeps one representative: the earliest item from the highest-priority source (so a Bloomberg headline is not lost behind a
later low-priority copy). Seed matching looks at EVERY member (see relevance.consolidate_clusters), so a wrong merge cannot hide an event.

Also here: `fair_cap`, the safety cap on the number of headlines per run. It spreads the cut evenly over days and sources instead of
dropping the earliest items (a hard "keep the last N" silently deleted most of a heavy Friday from Monday's window).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from app.domain import Article
from app.pipeline.embedding import Embedder, cosine

try:  # numpy ships with sentence-transformers; without it the pure-python loop below is used
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

_TOKEN = re.compile(r"[a-z0-9]{3,}")
_CANDIDATE_FLOOR = 0.5  # pairs below this cosine can never merge, not even through the Jaccard route


@dataclass(frozen=True)
class EventCluster:
    cluster_id: str
    representative: Article
    members: tuple[Article, ...]

    @property
    def sources(self) -> int:
        return len({m.source_id or m.source for m in self.members})


def _tokens(title: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(title.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def fair_cap(articles: list[Article], cap: int) -> tuple[list[Article], int]:
    """Keep at most `cap` articles, spread round-robin over (day, source) groups, P1 sources first. Returns (kept, cut count).

    Original order is preserved. Under the cap nothing is touched.
    """
    if cap <= 0 or len(articles) <= cap:
        return list(articles), 0
    groups: dict[tuple, list[int]] = defaultdict(list)
    for idx, a in enumerate(articles):
        groups[(a.source_priority != "P1", a.published_at.date(), a.source_id or a.source)].append(idx)
    ordered = [groups[k] for k in sorted(groups)]
    keep: list[int] = []
    rnd = 0
    while len(keep) < cap:
        progressed = False
        for g in ordered:
            if rnd < len(g):
                keep.append(g[rnd])
                progressed = True
                if len(keep) >= cap:
                    break
        if not progressed:
            break
        rnd += 1
    keep.sort()
    return [articles[i] for i in keep], len(articles) - len(keep)


def cluster_articles(
    articles: list[Article],
    embedder: Embedder,
    threshold: float,
    jaccard_threshold: float = 0.6,
) -> list[EventCluster]:
    if not articles:
        return []
    vectors = embedder.embed([a.title for a in articles])
    token_sets = [_tokens(a.title) for a in articles]
    parent = list(range(len(articles)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    if _np is not None and embedder.kind == "dense":
        matrix = _np.asarray(vectors, dtype=_np.float32)
        sims = matrix @ matrix.T
        for i, j in _np.argwhere(_np.triu(sims >= _CANDIDATE_FLOOR, k=1)):
            if sims[i, j] >= threshold or _jaccard(token_sets[i], token_sets[j]) >= jaccard_threshold:
                union(int(i), int(j))
    else:
        for i in range(len(articles)):
            for j in range(i + 1, len(articles)):
                if cosine(vectors[i], vectors[j]) >= threshold or _jaccard(token_sets[i], token_sets[j]) >= jaccard_threshold:
                    union(i, j)

    groups: dict[int, list[Article]] = {}
    for idx, article in enumerate(articles):
        groups.setdefault(find(idx), []).append(article)

    clusters: list[EventCluster] = []
    for members in groups.values():
        members.sort(key=lambda a: (a.source_priority != "P1", a.published_at))
        rep = members[0]
        clusters.append(EventCluster(f"c-{rep.article_id}", rep, tuple(members)))
    clusters.sort(key=lambda c: c.representative.published_at)
    return clusters
