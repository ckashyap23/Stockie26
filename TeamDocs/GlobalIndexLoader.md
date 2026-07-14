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

## Entry Point

```powershell
python scripts/Common/load_daily_index_data.py --no-local-output
```
