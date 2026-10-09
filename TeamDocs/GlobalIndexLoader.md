# Global Index Loader

Global-index OHLC rows are stored in `GlobalIndexOhlc` and joined into NIFTY
prediction features for audit and future decision layers.

## Current Contract

- US and Europe context use the latest completed western-market open-to-close
  move available before the NIFTY decision.
- Asia context uses the relevant close-window move available before NIFTY open.
- Strategy-level global suppressors are disabled.
- Global data still supports gap-guard safety checks around holiday or stale
  data windows.

## Scheduled Modes

The loader supports three scheduled modes; no date parameter is needed:

```powershell
# 3 AM IST - complete US/EUR 1d OHLC for yesterday
python scripts/Common/load_daily_index_data.py --mode us-eur

# 9:21 AM IST - partial Asia 5m OHLC through approximately 9:20
python scripts/Common/load_daily_index_data.py --mode asia-partial

# 3:45 PM IST - final Asia 1d OHLC for today
python scripts/Common/load_daily_index_data.py --mode asia-final

# Historical repair/backfill of genuine Asia final bars
python scripts/Common/load_daily_index_data.py --mode asia-backfill --start 2023-01-01 --end 2026-10-08
```

Asian return features require an exact-date partial row. The final-close anchor
must be strictly earlier than that partial session and no more than seven
calendar days old. Missing or stale inputs remain null; returns are never
forward-filled across sessions.

For backfill or manual runs with an explicit date range:

```powershell
python scripts/Common/load_daily_index_data.py --start 2026-01-01 --end 2026-07-16
```
