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

The loader supports the following scheduled and repair modes; no date parameter is needed:

```powershell
# 1 AM IST - completed European sessions
python -m technical_analysis_service run market.global -- --mode europe

# Run US separately after its close: approximately 2:45 IST during US daylight
# time and 3:45 IST during US standard time. 1 AM IST is too early.
python -m technical_analysis_service run market.global -- --mode us

# after 9:00 AM IST - partial Asia 5m OHLC through 9:00
python -m technical_analysis_service run market.global -- --mode asia-partial

# 3:45 PM IST - final Asia 1d OHLC for today
python -m technical_analysis_service run market.global -- --mode asia-final

# Historical repair/backfill of genuine Asia final bars
python -m technical_analysis_service run market.global -- --mode asia-backfill --start 2023-01-01 --end 2026-10-08

# Completed individual US/EUR daily-history repair
python -m technical_analysis_service run market.global -- --mode western-backfill --start 2024-01-01 --end 2026-10-19

# Exact recent 5m Asia partial backfill (Yahoo's documented intraday window)
python -m technical_analysis_service run market.global -- --mode asia-partial-backfill --start 2026-08-20 --end 2026-10-19

# Older approximation using only completed hourly bars (not exact 09:00 prices)
python -m technical_analysis_service run market.global -- --mode asia-partial-hourly-backfill --start 2024-10-21 --end 2026-08-31
```

Asian return features require an exact-date partial row. Each index now gets
its own `global_asia_partial_<INDEX>` and `global_asia_overnight_<INDEX>` column;
the mean columns are retained for compatibility. The common cutoff is 09:00
IST. Nikkei's eligible value is the last completed bar before its lunch break.
The final-close anchor
must be strictly earlier than that partial session and no more than seven
calendar days old. Missing or stale inputs remain null; returns are never
forward-filled across sessions.

For backfill or manual runs with an explicit date range:

```powershell
python -m technical_analysis_service run market.global -- --start 2026-01-01 --end 2026-07-16
```

These notes describe the local implementation. See [operations.md](operations.md) for the extracted service configuration and scheduling contract.
