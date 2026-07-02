"""
Render cron dispatcher — one job, three time windows.

Cron schedule (UTC, Mon-Fri):  30 2,6,11 * * 1-5
  02:30 UTC = 08:00 IST  pre-market  : global index → prediction → signal → paper entry
  06:30 UTC = 12:00 IST  midday      : option snapshot → paper monitor
  11:00 UTC = 16:30 IST  post-market : market refresh → option OHLC → paper monitor → report
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

def run(cmd: str) -> None:
    print(f"\n>>> {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=_ROOT)
    if result.returncode != 0:
        print(f"[WARN] exited {result.returncode}: {cmd}")

def pre_market() -> None:
    """02:30 UTC — all global markets closed, fire pre-market signal."""
    run("python scripts/Common/load_daily_index_data.py --no-local-output")
    run("python scripts/daily_NIFTY/daily_nifty_prediction.py")
    run("python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1")
    run("python scripts/daily_NIFTY/daily_paper_entry.py")

def midday() -> None:
    """06:30 UTC — NSE mid-session, refresh snapshots and monitor open trades."""
    run("python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py")
    run("python scripts/daily_NIFTY/daily_paper_monitor.py")

def post_market() -> None:
    """11:00 UTC — NSE closed, capture day's data and final paper state."""
    run("python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY")
    run("python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY")
    run("python scripts/daily_NIFTY/daily_paper_monitor.py")
    run("python scripts/daily_NIFTY/daily_paper_report.py")

def main() -> None:
    hour_utc = datetime.now(timezone.utc).hour
    minute_utc = datetime.now(timezone.utc).minute
    print(f"Dispatcher running at {hour_utc:02d}:{minute_utc:02d} UTC")

    if hour_utc == 2:
        pre_market()
    elif hour_utc == 6:
        midday()
    elif hour_utc == 11:
        post_market()
    else:
        print(f"No window matched for hour={hour_utc}. Exiting.")
        sys.exit(0)

if __name__ == "__main__":
    main()
