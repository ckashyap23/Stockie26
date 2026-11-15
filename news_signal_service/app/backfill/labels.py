"""Attach realised-outcome labels to the backfill scores:

    python -m app.backfill.labels [--features <production_features.csv>] [--data-dir var/backfill]

Reads `actual_trade_label` and `actual_trade_label_relaxed` from the technical-analysis features file and, from NIFTY_prediction.csv,
the system's `effective_prediction`; adds them (plus the signal date each came from) to every row of backfill_scores.csv and
backfill_state.json, so a later backfill run keeps them.

How the labels are defined (technical_analysis_service/analysis/cascade/dataset.py, with .env UNDERLYING_LOOKBACK_DAYS=1,
NIFTY_TARGET_PCT=0.006, NIFTY_TARGET_PCT_RELAXED=0.004): a row keyed `signal_date = S` describes a trade entered at the OPEN of the next
session (`next_trade_date`) and what the index did from that open during that session:
    CALL         high - open >= target   (target 0.6% strict, 0.4% relaxed)
    PUT          open - low  >= target
    BOTH         both reached            NO_POSITION   neither reached
So it is an intraday excursion from the open, not the close-to-close return.

Alignment: a backfill score for date D predicts day D (news before the open), so D is joined to the row whose **next_trade_date == D**
(signal_date is the previous session), never to the row whose signal_date == D. Dates with no such row (holidays or gaps in the features
data) are left blank: nothing is guessed.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from app.backfill.run import _load_state, _save_state, _write_csv
from app.config import SERVICE_ROOT, Settings

LABEL_COLUMNS = ["actual_trade_label", "actual_trade_label_relaxed", "label_from_signal_date"]
PREDICTION_COLUMNS = ["effective_prediction", "prediction_from_signal_date"]
DEFAULT_PREDICTIONS = SERVICE_ROOT.parent / "output" / "backtest" / "NIFTY" / "production" / "NIFTY_prediction.csv"
DEFAULT_FEATURES = SERVICE_ROOT.parent / "output" / "analysis" / "nifty_2026_strategy_misses_2026-11-09" / "production_features.csv"


def load_labels(features_csv: Path) -> dict[str, dict[str, str]]:
    """{target date: labels} keyed by the features row's next_trade_date."""
    out: dict[str, dict[str, str]] = {}
    with features_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            target = (row.get("next_trade_date") or "")[:10]
            if not target:
                continue
            out[target] = {
                "actual_trade_label": (row.get("actual_trade_label") or "").strip(),
                "actual_trade_label_relaxed": (row.get("actual_trade_label_relaxed") or "").strip(),
                "label_from_signal_date": (row.get("signal_date") or "")[:10],
            }
    return out


def load_predictions(prediction_csv: Path, features_csv: Path) -> dict[str, dict[str, str]]:
    """{trade date: prediction}. A prediction signalled on S applies to S's next trading day.

    That day is taken from the FEATURES calendar (signal_date -> next_trade_date), not from the prediction file's own `next_trade_date`:
    the prediction file derives it from the next row it happens to hold, so a missing signal day (e.g. 2026-11-02) makes the previous
    row point past the skipped session. A signal date absent from the features calendar falls back to the file's own value.
    """
    calendar: dict[str, str] = {}
    with features_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("signal_date") and row.get("next_trade_date"):
                calendar[row["signal_date"][:10]] = row["next_trade_date"][:10]
    out: dict[str, dict[str, str]] = {}
    with prediction_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            signal = (row.get("signal_date") or "")[:10]
            trade = calendar.get(signal) or (row.get("next_trade_date") or "")[:10]
            if signal and trade:
                out[trade] = {"effective_prediction": (row.get("effective_prediction") or "").strip(),
                              "prediction_from_signal_date": signal}
    return out


def apply_predictions(rows: dict[str, dict], predictions: dict[str, dict[str, str]]) -> tuple[int, list[str]]:
    matched, missing = 0, []
    for date_key, row in rows.items():
        found = predictions.get(date_key)
        if found and found["effective_prediction"]:
            row.update(found)
            matched += 1
        else:
            row.update({k: "" for k in PREDICTION_COLUMNS})
            missing.append(date_key)
    return matched, sorted(missing)


def apply_labels(rows: dict[str, dict], labels: dict[str, dict[str, str]]) -> tuple[int, list[str]]:
    """Fills the label columns of every score row in place. Returns (labelled count, dates with no label row)."""
    matched, missing = 0, []
    for date_key, row in rows.items():
        found = labels.get(date_key)
        if found and (found["actual_trade_label"] or found["actual_trade_label_relaxed"]):
            row.update(found)
            matched += 1
        else:
            row.update({k: "" for k in LABEL_COLUMNS})
            missing.append(date_key)
    return matched, sorted(missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS, help="NIFTY_prediction.csv (effective_prediction)")
    parser.add_argument("--data-dir", type=Path, default=None, help="default: <DATA_DIR>/backfill")
    args = parser.parse_args(argv)
    data_dir = args.data_dir or Settings().data_dir / "backfill"
    state_path, csv_path = data_dir / "backfill_state.json", data_dir / "backfill_scores.csv"
    if not args.features.exists():
        print(f"features file not found: {args.features}")
        return 2
    state = _load_state(state_path)
    if not state["rows"]:
        print(f"no scored dates in {state_path}")
        return 2
    matched, missing = apply_labels(state["rows"], load_labels(args.features))
    _save_state(state_path, state)
    _write_csv(csv_path, state)
    print(f"labelled {matched} of {len(state['rows'])} dates from {args.features.name}")
    print(f"no label for {len(missing)}: {', '.join(missing) or '-'}")
    if args.predictions.exists():
        p_matched, p_missing = apply_predictions(state["rows"], load_predictions(args.predictions, args.features))
        _save_state(state_path, state)
        _write_csv(csv_path, state)
        print(f"effective_prediction for {p_matched} of {len(state['rows'])} dates from {args.predictions.name}")
        print(f"no prediction for {len(p_missing)}: {', '.join(p_missing) or '-'}")
    else:
        print(f"predictions file not found, skipped: {args.predictions}")
    print(f"updated {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
