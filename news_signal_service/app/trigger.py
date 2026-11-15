"""Cron-friendly trigger for a hosted service (stdlib only):

    python -m app.trigger ingest                      # POST /v1/ingest  (every ~30 min)
    python -m app.trigger run [--date YYYY-MM-DD] [--cutoff HH:MM] [--replay] [--timeout 1500] [--csv out.csv]

Environment: SERVICE_URL (e.g. https://news-signal.up.railway.app) and TRIGGER_API_KEY (one of the service's API_KEYS).
`run` starts the background job, polls it until it finishes (exit 0 on success, 1 on failure/timeout), prints a one-line summary and,
with --csv, downloads the CSV.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _call(base: str, key: str, method: str, path: str, body: dict | None = None, opener=urllib.request.urlopen, timeout: int = 120):
    request = urllib.request.Request(
        base.rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def run_trigger(argv: list[str] | None = None, opener=urllib.request.urlopen, sleep=time.sleep, out=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["ingest", "run"])
    parser.add_argument("--date", default=None, help="target trading date (default: today, IST)")
    parser.add_argument("--cutoff", default=None, help="HH:MM IST window end override")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--timeout", type=int, default=1500, help="seconds to wait for the run to finish")
    parser.add_argument("--poll", type=int, default=15)
    parser.add_argument("--csv", default=None, help="save the CSV here after a successful run")
    args = parser.parse_args(argv)

    base, key = os.environ.get("SERVICE_URL", ""), os.environ.get("TRIGGER_API_KEY", "")
    if not base or not key:
        out("Set SERVICE_URL and TRIGGER_API_KEY.")
        return 2

    if args.command == "ingest":
        status, body = _call(base, key, "POST", "/v1/ingest", opener=opener)
        out(f"ingest HTTP {status}: {body.decode('utf-8', 'replace')[:400]}")
        return 0 if status == 200 else 1

    target = args.date or datetime.now(IST).date().isoformat()
    payload = {"target_date": target, "replay": args.replay, "cutoff_time": args.cutoff}
    status, body = _call(base, key, "POST", "/v1/signal/run", payload, opener)
    if status == 409:
        out(f"a run is already in progress: {body.decode('utf-8', 'replace')[:300]}")
        return 1
    if status != 202:
        out(f"could not start the run, HTTP {status}: {body.decode('utf-8', 'replace')[:400]}")
        return 1
    job = json.loads(body)
    out(f"started {job['job_id']}")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status, body = _call(base, key, "GET", f"/v1/runs/{job['job_id']}", opener=opener)
        job = json.loads(body) if status == 200 else job
        if job["status"] == "succeeded":
            out(f"done {target}: score {job['score']:+.4f} ({job['direction']}), confidence {job['confidence']:.2f}, {job['warnings']} warning(s)")
            if args.csv:
                status, csv_bytes = _call(base, key, "GET", f"/v1/signal/{target}/csv", opener=opener)
                if status != 200:
                    out(f"CSV download failed, HTTP {status}")
                    return 1
                with open(args.csv, "wb") as handle:
                    handle.write(csv_bytes)
                out(f"saved {args.csv}")
            return 0
        if job["status"] == "failed":
            out(f"run failed: {job.get('error')}")
            return 1
        sleep(args.poll)
    out(f"timed out after {args.timeout}s; the run may still finish: GET /v1/runs/{job['job_id']}")
    return 1


if __name__ == "__main__":
    sys.exit(run_trigger())
