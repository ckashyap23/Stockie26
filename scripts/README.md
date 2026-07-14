# Scripts

Scripts are operational entry points around the production database. Prefer the
daily wrappers for normal use and the Common/backfill scripts for maintenance.

## Daily NIFTY

- `daily_market_refresh.py`: refreshes daily underlying market data.
- `daily_nifty_signal.py`: orchestrates prediction and option selection.
- `daily_nifty_prediction.py`: runs only the production prediction cascade.
- `daily_option_selection.py`: runs only option selection for an existing signal.
- `daily_paper_entry.py`, `daily_paper_monitor.py`, `daily_paper_report.py`:
  manage paper execution lifecycle.
- Option instrument, snapshot, and OHLC scripts maintain option data inputs.

## Common Utilities

- `load_daily_index_data.py`: refreshes global-index OHLC/context.
- `calculate_underlying_features.py`: rebuilds underlying feature rows.
- `calculate_option_snapshot_calc.py`: computes option Greeks/IV fields.
- `analyze_precision_misses.py`: exports precision and recall miss CSVs with
  D-2/D-1/D/D+1/D+2 signal-feature context.
- `compare_strategy_family_layer.py`: compares cascade/watch behavior.
- `export_db_to_excel.py`: exports selected DB data for inspection.

## Backfills

`scripts/backfill_NIFTY/` contains historical loaders for NIFTY underlying,
option OHLC, option snapshots, volume, India VIX, and news sentiment.

## Notes

- Production jobs assume `.env` and Supabase connectivity.
- Render/cron commands should use repository-relative paths.
- Generated CSVs and logs belong under `output/`.
