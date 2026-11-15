"""Publish the daily news score into the technical-analysis database's "NiftyPrediction" table.

Contract (the two services share only this table, no imports):
  - the news score for TRADE date D is written on the row whose next_trade_date = D, i.e. signal_date = the previous trading day;
  - columns added here (if missing): news_score, news_direction, news_confidence, news_run_id, news_scored_at;
  - the technical service's own upsert never lists these columns, so its daily prediction run cannot overwrite them;
  - the score is informational: nothing in the prediction logic reads it.

If the signal row for D does not exist yet (the technical service creates it in its 15:47 refresh the day before), nothing is written and
the result says so; re-run `python -m app.publish` later to fill it.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

COLUMNS = (
    "news_score double precision",
    "news_direction varchar(20)",
    "news_confidence double precision",
    "news_run_id varchar(80)",
    "news_scored_at timestamptz",
)
SYMBOL, MODEL_VERSION = "NIFTY", "cascade_v1"


def connection_string(settings: Settings) -> str:
    return settings.news_score_db_url or settings.supabase_conn_str


def _connect(url: str):
    import psycopg2

    return psycopg2.connect(url, connect_timeout=15)


def ensure_columns(cur) -> None:
    for column in COLUMNS:
        cur.execute(f'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS {column}')


def publish_scores(settings: Settings, scores: list[dict[str, Any]], connect=_connect) -> list[dict[str, Any]]:
    """scores: [{trade_date, score, direction, confidence, run_id}] -> one result per trade date (written / reason)."""
    url = connection_string(settings)
    if not url:
        return [{"trade_date": str(s["trade_date"]), "written": False, "reason": "no database configured (NEWS_SCORE_DB_URL / SUPABASE_CONN_STR)"} for s in scores]
    results: list[dict[str, Any]] = []
    conn = connect(url)
    try:
        with conn.cursor() as cur:
            ensure_columns(cur)
            for item in scores:
                cur.execute(
                    'UPDATE "NiftyPrediction" SET news_score = %s, news_direction = %s, news_confidence = %s, news_run_id = %s, '
                    "news_scored_at = %s WHERE symbol = %s AND model_version = %s AND next_trade_date = %s",
                    (item["score"], item.get("direction"), item.get("confidence"), item.get("run_id"),
                     datetime.now(timezone.utc), SYMBOL, MODEL_VERSION, item["trade_date"]),
                )
                written = cur.rowcount > 0
                results.append({"trade_date": str(item["trade_date"]), "written": written,
                                "reason": "" if written else "no NiftyPrediction row with this next_trade_date yet (or the date is a holiday)"})
        conn.commit()
    finally:
        conn.close()
    return results


def publish_signal(settings: Settings, payload: dict, connect=_connect) -> dict[str, Any]:
    """Publish one stored signal payload (the output of a run)."""
    item = {"trade_date": payload["target_date"], "score": payload["score"], "direction": payload.get("direction"),
            "confidence": payload.get("confidence"), "run_id": payload.get("run_id")}
    return publish_scores(settings, [item], connect)[0]


def scores_from_backfill_csv(path: Path, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            day = date.fromisoformat(row["date"])
            if (start and day < start) or (end and day > end) or not row.get("score"):
                continue
            out.append({"trade_date": day, "score": float(row["score"]), "direction": row.get("direction") or None,
                        "confidence": float(row["confidence"]) if row.get("confidence") else None, "run_id": "backfill:" + (row.get("run_id") or "")})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write news scores into NiftyPrediction.news_score (by trade date).")
    parser.add_argument("--csv", type=Path, help="backfill_scores.csv: publish every date in it (optionally limited by --from/--to)")
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--date", type=date.fromisoformat, help="publish the stored live signal for this trade date")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.csv:
        results = publish_scores(settings, scores_from_backfill_csv(args.csv, args.start, args.end))
    elif args.date:
        from app.storage.file_store import FileSignalStore

        payload = FileSignalStore(settings.data_dir).load(args.date)
        if payload is None:
            print(f"no stored signal for {args.date}")
            return 1
        results = [publish_signal(settings, payload)]
    else:
        parser.error("give --csv or --date")
    missed = [r for r in results if not r["written"]]
    print(json.dumps({"written": len(results) - len(missed), "not_written": missed}, indent=1))
    return 0 if not missed else 3


if __name__ == "__main__":
    sys.exit(main())
