"""Score the configured LLM against a hand-labelled set of judging cases:   python -m app.eval.judge_benchmark

Data: data/eval/judge_golden.csv  (seed_id, headline, body, expected_direction -1/0/1, note). EDIT IT: it is the thing that
decides whether a model (or a prompt change) is good enough, and the cases are my labels, not ground truth.

Reports, per case and overall:
  direction accuracy        got == expected over all cases (0 means 'no scored signal')
  sign accuracy             of the cases that should score, did the sign match?
  false-positive rate       of the cases that should score ZERO (commentary, small players, in line, previews), how many scored?
  unscored                  answers the model failed to give (quota, bad JSON ...)
Uses a temporary cache, so it always makes real calls (about len(cases)/batch_size requests: mind free-tier quotas).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import SERVICE_ROOT, Settings
from app.domain import Article, RelevantArticle
from app.llm.cache import LlmCache
from app.llm.client import LlmClient, build_llm_client
from app.pipeline.llm_judge import LlmJudge
from app.pipeline.seeds import load_seeds
from app.tuning import load_tuning

GOLDEN = SERVICE_ROOT / "data" / "eval" / "judge_golden.csv"
IST = ZoneInfo("Asia/Kolkata")


def load_cases(path: Path = GOLDEN) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{**r, "expected_direction": int(r["expected_direction"])} for r in csv.DictReader(handle)]


def run(client: LlmClient | None, cases: list[dict] | None = None, note: str = "") -> dict:
    cases = cases or load_cases()
    seeds = load_seeds(SERVICE_ROOT / "data" / "event_seeds.csv")
    by_id = {s.seed_id: s for s in seeds}
    now = datetime.now(IST)
    items = []
    for n, case in enumerate(cases):
        seed = by_id[case["seed_id"]]
        article = Article(f"g{n}", "golden", f"http://golden/{n}", case["headline"], case["body"], now, now, "india", "rss",
                          content_source="summary")
        items.append(RelevantArticle(article, seed.seed_id, seed.family, seed.geography, 1.0, ()))
    with tempfile.TemporaryDirectory() as tmp:
        judge = LlmJudge(client, LlmCache(Path(tmp) / "bench.sqlite3"), seeds, load_tuning(), not_configured_reason=note)
        judgements, report = judge.judge(items)

    rows, should_score, sign_ok, should_zero, false_pos, exact = [], 0, 0, 0, 0, 0
    for case, j in zip(cases, judgements):
        exp, got = case["expected_direction"], j.direction
        exact += exp == got
        if exp != 0:
            should_score += 1
            sign_ok += got == exp
        else:
            should_zero += 1
            false_pos += got != 0
        rows.append({"seed": case["seed_id"], "expected": exp, "got": got, "confidence": j.confidence, "intensity": j.intensity,
                     "status": j.details.get("status"), "headline": case["headline"][:70], "rationale": j.rationale[:110]})
    n = len(cases)
    return {
        "model": getattr(client, "model", None), "cases": n, "direction_accuracy": exact / n,
        "sign_accuracy": sign_ok / should_score if should_score else None,
        "false_positive_rate": false_pos / should_zero if should_zero else None,
        "unscored": report.unscored, "calls": report.calls, "errors": report.errors, "rows": rows,
    }


def print_report(result: dict) -> None:
    for r in result["rows"]:
        mark = "ok " if r["expected"] == r["got"] else "ERR"
        print(f"{mark} {r['seed']:4} exp={r['expected']:+d} got={r['got']:+d} conf={r['confidence']:.2f} [{r['status']}] {r['headline']}")
    print(f"\nmodel: {result['model']}   cases: {result['cases']}   calls: {result['calls']}   unscored: {result['unscored']}")
    print(f"direction accuracy: {result['direction_accuracy']:.2f}")
    if result["sign_accuracy"] is not None:
        print(f"sign accuracy on cases that should score: {result['sign_accuracy']:.2f}")
    if result["false_positive_rate"] is not None:
        print(f"false-positive rate on cases that should be zero: {result['false_positive_rate']:.2f}")
    for e in dict.fromkeys(result["errors"]):
        print("error:", e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=SERVICE_ROOT / "data" / "eval" / "judge_benchmark_latest.json")
    args = parser.parse_args(argv)
    client, note = build_llm_client(Settings())
    if client is None:
        print(f"LLM not configured: {note}")
        return 2
    result = run(client)
    print_report(result)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
