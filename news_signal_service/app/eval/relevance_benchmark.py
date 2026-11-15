"""Benchmark how well each similarity model links a headline to the right seed event.

    python -m app.eval.relevance_benchmark [--embedders tfidf finbert st:sentence-transformers/all-MiniLM-L6-v2 ...]

Data: data/eval/relevance_golden.csv (headline, acceptable seeds or none). For every embedder and every seed-document
recipe we score each headline against all seeds, then report:

  top1 / top3   of headlines that DO match a seed: is the best (top-3) seed an acceptable one?
  AUROC         threshold-free: do relevant headlines get a higher best-similarity than irrelevant ones?
  best F1       strict decision "relevant if best similarity >= t" with the best seed also being acceptable;
                reported with that threshold t, precision, recall and the false-positive rate on irrelevant headlines
  boost sweep   same, after adding a keyword-hit boost to the similarity (the proposed gate: similarity decides,
                keywords only lift)

and, for reference, the current keyword gate.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import SERVICE_ROOT
from app.domain import Article, SeedEvent
from app.pipeline.embedding import Embedder, cosine, make_embedder
from app.pipeline.exclusion import first_exclusion
from app.pipeline.relevance import _keyword_scores, _terms, seed_parts
from app.pipeline.seeds import load_seeds
from app.tuning import load_tuning

GOLDEN = SERVICE_ROOT / "data" / "eval" / "relevance_golden.csv"
DEFAULT_EMBEDDERS = [
    "tfidf", "finbert", "st:sentence-transformers/all-MiniLM-L6-v2", "st:BAAI/bge-small-en-v1.5",
    "st:sentence-transformers/all-mpnet-base-v2",
]
# A recipe turns a seed into one or more texts; the seed's similarity to a headline is the MAX over its texts.
DOC_RECIPES = {
    "event+aliases": lambda s: [" ".join([s.event_type, *s.aliases])],
    "event+aliases+hypothesis": lambda s: [" ".join([s.event_type, *s.aliases, s.hypothesis])],
    "parts: event | event:alias": lambda s: [s.event_type, *[f"{s.event_type}: {a}" for a in s.aliases]],
    "parts: +hypothesis": lambda s: [s.event_type, s.hypothesis, *[f"{s.event_type}: {a}" for a in s.aliases]],
    "PRODUCTION: parts, NIFTY stripped": lambda s: seed_parts(s, ("NIFTY",)),  # exactly what the pipeline compares against
}
BOOSTS = (0.0, 0.2, 0.3)
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Item:
    headline: str
    expected: frozenset[str]
    kind: str
    hard: bool


def load_golden(path: Path = GOLDEN) -> list[Item]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            Item(r["headline"], frozenset(x for x in r["expected_seeds"].split(";") if x), r["kind"], r["hard"] == "1")
            for r in csv.DictReader(handle)
        ]


def keyword_matrix(items: list[Item], seeds: tuple[SeedEvent, ...]) -> list[list[float]]:
    """Normalised keyword evidence min(1, score/2) per (headline, seed), using the production keyword scorer."""
    tuning = load_tuning()
    terms = [(s, _terms(s)) for s in seeds]
    now = datetime.now(IST)
    out = []
    for it in items:
        article = Article("x", "s", "u", it.headline, "", now, now, "india", "rss")
        scored = _keyword_scores(article, terms, tuning)
        out.append([min(1.0, scored.get(s.seed_id, (0.0, ()))[0] / 2.0) for s in seeds])
    return out


def exclusion_mask(items: list[Item], seeds: tuple[SeedEvent, ...]) -> list[list[bool]]:
    """True where a seed's exclude cue appears in the headline: production removes those candidates before scoring."""
    return [[first_exclusion(it.headline, s.exclude_cues) is not None for s in seeds] for it in items]


def apply_mask(matrix: list[list[float]], mask: list[list[bool]]) -> list[list[float]]:
    return [[0.0 if m else v for v, m in zip(row, mrow)] for row, mrow in zip(matrix, mask)]


def auroc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def evaluate(matrix: list[list[float]], items: list[Item], seed_ids: list[str]) -> dict:
    best = [max(range(len(row)), key=row.__getitem__) for row in matrix]
    top = [row[b] for row, b in zip(matrix, best)]
    pos = [i for i, it in enumerate(items) if it.expected]
    neg = [i for i, it in enumerate(items) if not it.expected]
    top3 = [sorted(range(len(r)), key=r.__getitem__, reverse=True)[:3] for r in matrix]
    correct = {i: seed_ids[best[i]] in items[i].expected for i in pos}

    def stats(t: float) -> tuple[float, float, float, float]:
        predicted = [i for i in range(len(items)) if top[i] >= t]
        tp = sum(1 for i in predicted if i in correct and correct[i])
        precision = tp / len(predicted) if predicted else 0.0
        recall = tp / len(pos) if pos else 0.0
        fpr = sum(1 for i in neg if top[i] >= t) / len(neg) if neg else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return f1, precision, recall, fpr

    thresholds = sorted({round(v, 4) for v in top})
    best_t = max(thresholds, key=lambda t: stats(t)[0])
    f1, precision, recall, fpr = stats(best_t)
    hard = [i for i, it in enumerate(items) if it.hard]
    return {
        "top1": sum(correct.values()) / len(pos),
        "top3": sum(any(seed_ids[j] in items[i].expected for j in top3[i]) for i in pos) / len(pos),
        "top1_real": _rate([correct[i] for i in pos if items[i].kind == "real"]),
        "top1_authored": _rate([correct[i] for i in pos if items[i].kind == "authored"]),
        "auroc": auroc([top[i] for i in pos], [top[i] for i in neg]),
        "f1": f1, "t": best_t, "precision": precision, "recall": recall, "fpr": fpr,
        "hard_fp": _rate([top[i] >= best_t for i in hard]),
    }


def _rate(values: list[bool]) -> float:
    return sum(values) / len(values) if values else float("nan")


def run(embedder_specs: list[str], out_path: Path | None = None) -> list[dict]:
    items = load_golden()
    seeds = load_seeds(SERVICE_ROOT / "data" / "event_seeds.csv")
    seed_ids = [s.seed_id for s in seeds]
    kw = keyword_matrix(items, seeds)
    mask = exclusion_mask(items, seeds)
    results: list[dict] = []

    # Reference: the current keyword gate (kw score >= 1.0 <=> normalised >= 0.5), best seed = most keyword evidence.
    base = evaluate(apply_mask(kw, mask), items, seed_ids)
    results.append({"embedder": "keyword gate (current)", "doc": "-", "boost": "-", **base})

    for spec in embedder_specs:
        try:
            embedder: Embedder = make_embedder(spec)
        except Exception as exc:  # noqa: BLE001 - a model that cannot load is skipped, not fatal
            print(f"  skipped {spec}: {type(exc).__name__}: {str(exc)[:90]}", file=sys.stderr)
            continue
        for doc_name, recipe in DOC_RECIPES.items():
            parts = [recipe(s) for s in seeds]
            flat = [text for group in parts for text in group]
            vectors = embedder.embed(flat + [it.headline for it in items])
            part_vecs, head_vecs = vectors[: len(flat)], vectors[len(flat):]
            owners, k = [], 0
            for group in parts:
                owners.append(part_vecs[k:k + len(group)])
                k += len(group)
            sim = [[max(0.0, max(cosine(h, pv) for pv in group)) for group in owners] for h in head_vecs]
            for boost in BOOSTS:
                matrix = apply_mask([[min(1.0, s + boost * k) for s, k in zip(sr, kr)] for sr, kr in zip(sim, kw)], mask)
                results.append({"embedder": spec.split("/")[-1], "doc": doc_name, "boost": boost,
                                **evaluate(matrix, items, seed_ids)})
    if out_path:
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def print_table(results: list[dict]) -> None:
    head = f"{'embedder':32} {'seed doc':26} {'boost':>5} {'top1':>5} {'top3':>5} {'t1real':>6} {'t1auth':>6} {'AUROC':>6} {'F1':>5} {'thr':>6} {'prec':>5} {'rec':>5} {'FPR':>5} {'hardFP':>6}"
    print(head)
    print("-" * len(head))
    for r in results:
        print(f"{r['embedder'][:32]:32} {r['doc'][:26]:26} {str(r['boost']):>5} {r['top1']:5.2f} {r['top3']:5.2f} "
              f"{r['top1_real']:6.2f} {r['top1_authored']:6.2f} {r['auroc']:6.2f} {r['f1']:5.2f} {r['t']:6.3f} "
              f"{r['precision']:5.2f} {r['recall']:5.2f} {r['fpr']:5.2f} {r['hard_fp']:6.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--embedders", nargs="*", default=DEFAULT_EMBEDDERS)
    parser.add_argument("--out", type=Path, default=SERVICE_ROOT / "data" / "eval" / "relevance_benchmark_latest.json")
    args = parser.parse_args(argv)
    results = run(args.embedders, args.out)
    print_table(results)
    print(f"\nsaved -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
