"""
Fetch global index OHLC data and persist it to Supabase.

Supports two cron-friendly modes (no --date param needed):
  --mode us-eur        Run at 3 AM IST: fetches complete 1d OHLC for US + EUR
                       indices for the previous trading day (d-1). Asia indices
                       are skipped entirely.
  --mode asia-partial  Run at 9 AM IST: fetches partial 5m OHLC (open → 9:20 AM IST)
                       for Asia indices for today. US/EUR indices are skipped.

Without --mode, all indices are fetched for the requested date range (default
dev/backfill usage with optional --start / --end).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.global_index_loader import (
    ASIA_PARTIAL_INDEXES,
    DEFAULT_GLOBAL_INDEX_OUTPUT_DIR,
    GLOBAL_INDEX_UNIVERSE,
    fetch_global_index_ohlc,
    write_global_index_ohlc_csv,
)

load_dotenv(project_root / ".env")

# Regions treated as "western" (complete 1d bars, d-1 cron).
_WESTERN_REGIONS = {"United States", "United Kingdom", "Germany", "France", "Europe"}


def _previous_weekday(d: date) -> date:
    """Return the most recent weekday before `d` (skips Sat/Sun)."""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:  # 5=Sat, 6=Sun
        prev -= timedelta(days=1)
    return prev


def run_load_daily_index_data(
    start_date: date | None = None,
    end_date: date | None = None,
    lookback: int = 7,
    write_local_output: bool = True,
    mode: str | None = None,
) -> dict:
    """
    mode options:
      None            – full universe, caller-specified or default date range
      "us-eur"        – western indices only, date = previous weekday (d-1)
      "asia-partial"  – ASIA_PARTIAL_INDEXES only, date = today (5m partial)
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")

    today = date.today()

    # ── Mode: select index subset and auto-resolve dates ─────────────────────
    if mode == "us-eur":
        index_universe = tuple(
            idx for idx in GLOBAL_INDEX_UNIVERSE
            if idx.get("region") in _WESTERN_REGIONS
        )
        target = _previous_weekday(today)
        resolved_start = resolved_end = target
        print(f"[mode=us-eur] Fetching western 1d OHLC for {target} "
              f"({len(index_universe)} indices) ...")

    elif mode == "asia-partial":
        index_universe = tuple(
            idx for idx in GLOBAL_INDEX_UNIVERSE
            if idx["index_code"] in ASIA_PARTIAL_INDEXES
        )
        resolved_start = resolved_end = today
        print(f"[mode=asia-partial] Fetching Asia 5m partial OHLC for {today} "
              f"({len(index_universe)} indices, cutoff 9:20 AM IST) ...")

    else:
        index_universe = GLOBAL_INDEX_UNIVERSE
        resolved_end = end_date or today
        resolved_start = start_date or (resolved_end - timedelta(days=lookback - 1))
        if resolved_start > resolved_end:
            raise ValueError("start_date must be <= end_date")
        print(f"Fetching global index OHLC {resolved_start} -> {resolved_end} ...")

    rows = fetch_global_index_ohlc(resolved_start, resolved_end, index_universe=index_universe)
    print(f"Fetched {len(rows)} global index OHLC rows")

    settings = get_settings()
    db = get_database_client(settings)
    if getattr(db, "db_kind", "") != "postgres":
        raise RuntimeError(
            "load_daily_index_data.py currently supports the Supabase/postgres provider only. "
            "Set DATABASE_PROVIDER=supabase."
        )

    db.connect()
    try:
        upserted = db.upsert_global_index_ohlc(rows)
    finally:
        db.close()
    print(f"Upserted {upserted} rows into GlobalIndexOhlc")

    local_output_path = None
    if write_local_output:
        try:
            local_output_path = write_global_index_ohlc_csv(rows, resolved_end)
            if local_output_path:
                print(f"Local output written to {local_output_path}")
        except Exception as exc:  # noqa: BLE001 - local output is optional for Render/cron.
            print(f"Skipping local global index OHLC output: {exc}")

    return {
        "mode": mode,
        "start_date": resolved_start.isoformat(),
        "end_date": resolved_end.isoformat(),
        "rows": len(rows),
        "upserted": upserted,
        "local_output_path": str(local_output_path) if local_output_path else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch global index OHLC data into Supabase")
    parser.add_argument(
        "--mode",
        choices=["us-eur", "asia-partial"],
        default=None,
        help=(
            "Cron mode: 'us-eur' fetches western 1d OHLC for yesterday (run at 3 AM IST); "
            "'asia-partial' fetches Asia 5m partial for today (run at 9 AM IST). "
            "Omit for full-universe backfill with --start / --end."
        ),
    )
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (ignored when --mode is set)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (ignored when --mode is set)")
    parser.add_argument("--lookback", type=int, default=7, help="Calendar-day lookback when --start is omitted")
    parser.add_argument(
        "--no-local-output",
        action="store_true",
        help=f"Skip best-effort CSV output under {DEFAULT_GLOBAL_INDEX_OUTPUT_DIR}",
    )
    args = parser.parse_args()

    result = run_load_daily_index_data(
        start_date=date.fromisoformat(args.start) if args.start else None,
        end_date=date.fromisoformat(args.end) if args.end else None,
        lookback=args.lookback,
        write_local_output=not args.no_local_output,
        mode=args.mode,
    )
    print(result)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()