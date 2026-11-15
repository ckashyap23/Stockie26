"""Run the whole pipeline over a past date range and keep a score per trading date.

    python -m app.backfill.run --from 2026-09-09 --to 2026-11-09 [--resume] [--skip-fetch] [--limit N]

Phase 1 (fetch): rebuild historical headlines for each date from Google News (app/backfill/gnews.py) into the article archive.
Phase 2 (judge): for each date, replay the pipeline from the archive (record=True), which writes the full per-stage audit folder
    <data-dir>/runs/<date>/<HHMMSS>/  (headlines, clusters, matches, judgements, llm_calls, aggregation, signal, meta)
and appends one row to <data-dir>/backfill_scores.csv (and signals/<date>.json).

Resumable: a date whose LLM answers were all obtained is skipped with --resume; a 'partial' date (quota ran out) is re-run and the
LLM cache supplies everything already answered. Stops early if the LLM quota looks exhausted, so nothing is silently half-scored.

Read the results with these limits in mind: historical headlines carry only a date, so each date uses the items dated from the previous
trading day through the day before it (nothing dated the target day), headlines only (no article bodies), partial coverage; weekdays
include exchange holidays (not modelled).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.backfill import gnews
from app.config import SERVICE_ROOT, Settings
from app.pipeline.orchestrator import run_signal
from app.storage.archive import ArticleArchive
from app.storage.file_store import FileSignalStore

IST = ZoneInfo("Asia/Kolkata")
COLUMNS = [
    "date", "weekday", "score", "direction", "magnitude", "confidence", "status", "news_items", "events_matched",
    "llm_judged", "llm_vetoed", "llm_unscored", "llm_calls", "top_groups", "top_headline", "run_id",
    # added by `python -m app.backfill.labels` (realised outcome from the features file)
    "actual_trade_label", "actual_trade_label_relaxed", "label_from_signal_date",
    "effective_prediction", "prediction_from_signal_date",
]
QUOTA_WORDS = ("rate limited", "quota")


def weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"fetched": {}, "rows": {}}


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _write_csv(path: Path, state: dict) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for day in sorted(state["rows"]):
            writer.writerow({k: state["rows"][day].get(k, "") for k in COLUMNS})


def row_from_result(target: date, result: dict) -> dict:
    js = result.get("judge_summary", {})
    unscored = js.get("llm_unscored", 0)
    if result["article_count_fetched"] == 0:
        status = "no_news"
    elif unscored:
        status = "partial"
    else:
        status = "complete"
    evidence = result.get("evidence") or []
    scored = [e for e in evidence if e["signal"]]
    return {
        "date": target.isoformat(), "weekday": target.strftime("%a"), "score": result["score"], "direction": result["direction"],
        "magnitude": result["magnitude"], "confidence": result["confidence"], "status": status,
        "news_items": result["article_count_fetched"], "events_matched": result["article_count_relevant"],
        "llm_judged": js.get("llm_judged", 0), "llm_vetoed": js.get("llm_vetoed", 0),
        "llm_unscored": unscored, "llm_calls": js.get("llm_calls", 0),
        "top_groups": "; ".join(f"{g['group']}:{g['score']:+.2f}" for g in result["groups"][:3]),
        "top_headline": scored[0]["headline"][:120] if scored else "", "run_id": result.get("run_id", ""),
    }


def fetch_phase(dates: list[date], settings: Settings, state: dict, state_path: Path, queries: list[str],
                workers: int, interval: float, get=None) -> None:
    todo = [d for d in dates if d.isoformat() not in state["fetched"]]
    if not todo:
        print("fetch: nothing to do (all dates already fetched)")
        return
    archive = ArticleArchive(settings.archive_file())
    pacer = gnews._Pacer(interval)
    kwargs = {"get": get} if get else {}
    print(f"fetch: {len(todo)} dates x {len(queries)} queries, {workers} workers, ~{(1 / interval if interval else float("inf")):.1f} requests/s", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(gnews.fetch_target, d, queries, pacer, **kwargs): d for d in todo}
        for future in as_completed(futures):
            d = futures[future]
            articles = future.result()
            archive.record(articles)
            state["fetched"][d.isoformat()] = len(articles)
            _save_state(state_path, state)
            done += 1
            print(f"  fetched {d} ({d.strftime('%a')}): {len(articles)} headlines  [{done}/{len(todo)}]", flush=True)


def judge_phase(dates: list[date], settings: Settings, state: dict, state_path: Path, csv_path: Path, resume: bool) -> int:
    store = FileSignalStore(settings.data_dir)
    quota_hits = 0
    for n, d in enumerate(dates, start=1):
        key = d.isoformat()
        if resume and state["rows"].get(key, {}).get("status") in {"complete", "no_news"}:
            continue
        result = run_signal(d, settings, replay=True, record=True).to_dict()
        store.save(d, result)
        row = row_from_result(d, result)
        state["rows"][key] = row
        _save_state(state_path, state)
        _write_csv(csv_path, state)
        print(f"  [{n}/{len(dates)}] {key} {row['weekday']}: score {row['score']:+.3f} {row['direction']:8} "
              f"conf {row['confidence']:.2f}  items={row['news_items']} matched={row['events_matched']} "
              f"llm judged={row['llm_judged']} vetoed={row['llm_vetoed']} unscored={row['llm_unscored']}  [{row['status']}]", flush=True)
        errors = " ".join(result.get("judge_summary", {}).get("llm_errors", [])).lower()
        quota_hits = quota_hits + 1 if (row["llm_unscored"] and any(w in errors for w in QUOTA_WORDS)) else 0
        if quota_hits >= 2:
            print("\nSTOPPED: the LLM quota looks exhausted (two dates in a row came back unscored). "
                  "Re-run later with --resume; answers already obtained are cached.", flush=True)
            return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    today = datetime.now(IST).date()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=today - timedelta(days=61))
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=today)
    parser.add_argument("--data-dir", type=Path, default=None, help="default: <DATA_DIR>/backfill")
    parser.add_argument("--queries", type=Path, default=SERVICE_ROOT / "data" / "backfill_queries.yaml")
    parser.add_argument("--resume", action="store_true", help="skip dates already complete")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="only the first N dates (dry run)")
    parser.add_argument("--embedder", default=None, help="override EMBEDDER for this run (e.g. sentence_transformers)")
    parser.add_argument("--llm-provider", default=None, help="override LLM_PROVIDER for this run (e.g. gemini)")
    parser.add_argument("--llm-model", default=None, help="override LLM_MODEL for this run")
    parser.add_argument("--fetch-workers", type=int, default=3)
    parser.add_argument("--request-interval", type=float, default=0.5, help="seconds between Google News requests (all workers)")
    args = parser.parse_args(argv)

    base = Settings()
    data_dir = args.data_dir or base.data_dir / "backfill"
    data_dir.mkdir(parents=True, exist_ok=True)
    overrides = {k: v for k, v in (("embedder", args.embedder), ("llm_provider", args.llm_provider), ("llm_model", args.llm_model)) if v}
    settings = Settings(data_dir=data_dir, **overrides)
    if settings.embedder.strip().lower() == "tfidf":
        print("WARNING: EMBEDDER=tfidf is the weak fallback (benchmark F1 0.46 vs 0.73); use --embedder sentence_transformers", flush=True)
    dates = weekdays(args.start, args.end)[: args.limit]
    state_path, csv_path = data_dir / "backfill_state.json", data_dir / "backfill_scores.csv"
    state = _load_state(state_path)
    print(f"backfill {dates[0]} .. {dates[-1]}: {len(dates)} weekdays -> {data_dir}", flush=True)

    if not args.skip_fetch:
        fetch_phase(dates, settings, state, state_path, gnews.load_queries(args.queries), args.fetch_workers, args.request_interval)
    code = 0
    if not args.skip_judge:
        print(f"judge: provider={settings.llm_provider} model={settings.llm_model or '(default)'} embedder={settings.embedder}", flush=True)
        code = judge_phase(dates, settings, state, state_path, csv_path, args.resume)
    _write_csv(csv_path, state)
    print(f"\nscores: {csv_path}\nrun folders: {data_dir / 'runs'}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
