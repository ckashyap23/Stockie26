# Global Index Loader

Global-index OHLC rows are stored in `GlobalIndexOhlc` and joined into NIFTY
prediction features for audit and future decision layers.

## Current Contract

- US and Europe context use the latest completed western-market open-to-close
  move available before the NIFTY decision.
- Asia context uses the relevant close-window move available before NIFTY open.
- Strategy-level global suppressors are disabled.
- Paper entry still has a secondary gap-gate safety check for stale or holiday
  data.

## Cron Modes

The loader script supports two scheduled modes — no date parameter needed:

```powershell
# 3 AM IST — complete US/EUR 1d OHLC for yesterday
python scripts/Common/load_daily_index_data.py --mode us-eur

# 9 AM IST — partial Asia 5m OHLC for today (open to 9:20 AM IST)
python scripts/Common/load_daily_index_data.py --mode asia-partial
```

For backfill or manual runs with explicit date range:

```powershell
python scripts/Common/load_daily_index_data.py --start 2026-01-01 --end 2026-07-16
```
