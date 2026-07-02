# Global Index Loader

Global-index data is an active production input. The pre-market refresh is
joined into NIFTY features and persisted on `NiftyPrediction`; the paper-entry
gap gate is a secondary safety check.

Loads global index OHLC rows into `GlobalIndexOhlc`.

## Run

Default refresh:

```powershell
python scripts/Common/load_daily_index_data.py
```

Explicit range:

```powershell
python scripts/Common/load_daily_index_data.py --start 2025-01-01 --end 2026-06-25
```

Render cron:

```bash
python scripts/Common/load_daily_index_data.py --no-local-output
```

## Output

DB table:

```text
GlobalIndexOhlc
```

Optional local CSV:

```text
output/intelligence/global_index_ohlc/DD-MM-YYYY/global_index_ohlc.csv
```

## Notes

- Source: Yahoo Finance via `src/data_manager/global_index_loader.py`.
- Rows are keyed by `(index_code, trade_date, source)`.
- Use `--no-local-output` on Render to avoid unnecessary artifacts.

## Quick SQL Check

```sql
SELECT index_code, count(*) AS rows, max(trade_date) AS latest
FROM "GlobalIndexOhlc"
GROUP BY index_code
ORDER BY index_code;
```
