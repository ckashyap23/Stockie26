"""Backfill Yahoo daily Brent, WTI, US 10-year yield, and USD/INR levels."""
from __future__ import annotations

from technical_analysis_service.runtime import SERVICE_ROOT, load_environment

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

project_root = SERVICE_ROOT
load_environment()

from technical_analysis_service.data_manager.macro_factor_loader import (  # noqa: E402
    fetch_market_macro_closes,
    upsert_macro_factors,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if start > end:
        raise ValueError("start must be on or before end")
    rows = fetch_market_macro_closes(start, end)
    count = upsert_macro_factors(rows)
    print({"start": str(start), "end": str(end), "rows_upserted": count})


if __name__ == "__main__":
    main()