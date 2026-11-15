"""Calibrate the news score against realised NIFTY returns.

    python -m app.calibration.fit --prices nifty_close.csv [--signals-dir var/signals] [--out var/calibration.json]

prices CSV: columns `date` (YYYY-MM-DD) and `close`, one row per trading session (ALL sessions - quiet
days included; the 164-session labelled file is selection-biased and must not be used alone).
Signals: the JSON files written by POST /v1/signal/run, one per target_date. Build history by running
the service daily, or by replaying archived news with `"replay": true`.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.calibration.model import fit


def load_returns(prices_csv: Path) -> dict[str, float]:
    with prices_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = sorted((r["date"].strip(), float(r["close"])) for r in csv.DictReader(handle))
    return {d: c / prev - 1 for (_, prev), (d, c) in zip(rows, rows[1:])}


def load_scores(signals_dir: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for path in sorted(signals_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        out[payload["target_date"]] = float(payload["score"])
    return out


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--signals-dir", type=Path, default=settings.data_dir / "signals")
    parser.add_argument("--out", type=Path, default=settings.calibration_file())
    parser.add_argument("--min-rows", type=int, default=60)
    args = parser.parse_args(argv)

    returns, scores = load_returns(args.prices), load_scores(args.signals_dir)
    rows = [(d, scores[d], returns[d]) for d in sorted(scores.keys() & returns.keys())]
    try:
        calibration = fit(rows, min_rows=args.min_rows)
    except ValueError as exc:
        print(f"Not calibrated: {exc}", file=sys.stderr)
        return 1
    calibration.save(args.out)
    print(json.dumps(json.loads(args.out.read_text(encoding="utf-8")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
