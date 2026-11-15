"""Pre-open data-quality check: report missing and stale inputs before the decision.

Run shortly before the 09:12 IST decision (after the Asia partial and GIFT
pre-open loads). It reads the inputs a morning decision for trading day N depends
on and reports, per field, OK / MISSING / STALE / NOT_FINAL / LATE.

Exit code: 0 = OK or WARN (only optional fields have problems), 2 = FAIL (a
required field is missing/stale, so a downstream layer should fall back to
Layer 1 only), 1 = the check itself could not run.

Usage:
    python scripts/daily_NIFTY/daily_data_quality_check.py
    python scripts/daily_NIFTY/daily_data_quality_check.py --decision-date 2026-10-28 --json
"""
from __future__ import annotations

from technical_analysis_service.runtime import SERVICE_ROOT, load_environment

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

project_root = SERVICE_ROOT
load_environment()

from technical_analysis_service.common.config import get_settings
from technical_analysis_service.data_manager.data_quality import (
    IST,
    PremarketInputs,
    run_checks,
    summarize,
)
from technical_analysis_service.data_manager.db.client_factory import get_database_client
from technical_analysis_service.analysis.cascade.global_index_features import load_global_index_rows_from_db

FEATURE_COLUMNS = (
    "pcr_oi", "oi_regime", "futures_oi",
    "breadth_advance_decline_ratio", "breadth_new_high_ratio",
    "banknifty_rel_strength", "volume_day",
)
MACRO_COLUMNS = ("brent_close", "wti_close", "us10y_level", "usdinr_close", "india_vix", "fii_net_cr", "dii_net_cr")


def _rows(cur) -> list[dict]:
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def load_inputs(decision_date: date) -> PremarketInputs:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        prev_session = db.get_previous_trading_day(decision_date, exchange="NSE")
        if prev_session is None:
            raise RuntimeError(f"TradingCalendar has no NSE session before {decision_date}")
        with db.conn.cursor() as cur:
            cur.execute(
                'SELECT trade_date, gift_0910, gift_1515, fetched_at FROM "GiftNiftySnapshot" '
                "WHERE trade_date = ANY(%s)",
                ([decision_date, prev_session],),
            )
            gift = {row["trade_date"]: row for row in _rows(cur)}
            cur.execute(
                f'SELECT factor_date, {", ".join(MACRO_COLUMNS)} FROM "MacroFactorDaily" '
                "WHERE factor_date BETWEEN %s AND %s ORDER BY factor_date",
                (prev_session - timedelta(days=21), prev_session),
            )
            macro = pd.DataFrame(_rows(cur))
            cur.execute(
                f'SELECT {", ".join(FEATURE_COLUMNS)} FROM "SignalFeatureDaily" '
                "WHERE symbol = 'NIFTY' AND signal_date = %s ORDER BY feature_version LIMIT 1",
                (prev_session,),
            )
            feature_rows = _rows(cur)
    finally:
        db.close()

    global_rows = load_global_index_rows_from_db(
        decision_date - timedelta(days=21), decision_date + timedelta(days=1)
    )
    now = datetime.now(IST)
    return PremarketInputs(
        decision_date=decision_date,
        prev_session=prev_session,
        # The LATE check only makes sense for a live run, not a historical replay.
        now=now if now.date() == decision_date else None,
        gift=gift,
        global_rows=global_rows,
        macro=macro,
        features=feature_rows[0] if feature_rows else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--decision-date", help="Trading day N to check (default: today IST).")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    args = parser.parse_args()
    decision_date = (
        date.fromisoformat(args.decision_date) if args.decision_date else datetime.now(IST).date()
    )

    try:
        inputs = load_inputs(decision_date)
        findings = run_checks(inputs)
    except Exception as exc:  # noqa: BLE001 - report as a check failure, not a traceback
        print(f"[ERROR] data-quality check could not run: {exc}")
        return 1

    summary = summarize(findings)
    if args.json:
        print(json.dumps({
            "decision_date": str(decision_date),
            "prev_session": str(inputs.prev_session),
            **summary,
            "findings": [f.__dict__ for f in findings],
        }, default=str))
    else:
        print(f"Data quality for decision {decision_date} (previous session {inputs.prev_session})")
        for f in findings:
            flag = "" if f.ok else ("  <-- REQUIRED" if f.required else "")
            print(f"  {f.status:<9} {f.field:<32} {f.detail}{flag}")
        print(f"Overall: {summary['status']} "
              f"({len(summary['required_problems'])} required, "
              f"{len(summary['optional_problems'])} optional problems)")
    return 2 if summary["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
