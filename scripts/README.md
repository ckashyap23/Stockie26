# Scripts

Scripts are operational entry points around the production database. Prefer the
daily wrappers for normal use and the Common/backfill scripts for maintenance.

## Daily NIFTY

- `daily_market_refresh.py`: refreshes daily underlying market data; automatically
  chains `daily_nifty_prediction.py` (5-day lookback) on completion.
- `daily_open_gap.py`: 9:20 AM IST cron — fetches NIFTY and GIFT NIFTY 9:15 AM
  5m candles, saves OHLC to `UnderlyingCandle5m` / `GiftNiftySnapshot`, then
  computes 7 open-gap features (`nifty_gap_pct`, `nifty_drift_pct`, `gift_gap_pct`,
  `gap_confirmed`, `gap_fade`, `gap_open_atr`, `gift_gap_atr`) and upserts them
  to `SignalFeatureDaily` for signal_date = D-1. Supports `--start`/`--end` for backfill.
- `daily_nifty_signal.py`: orchestrates prediction and option selection.
- `daily_nifty_prediction.py`: runs only the production prediction cascade.
- `daily_option_selection.py`: runs only option selection for an existing signal.
- `daily_paper_entry.py`, `daily_paper_monitor.py`, `daily_paper_report.py`:
  manage paper execution lifecycle.
- Option instrument, snapshot, and OHLC scripts maintain option data inputs.
  `daily_NIFTYoption_OHLC.py` automatically chains `pipeline_upsert_option_selections.py`
  and `pipeline_backtest_pnl.py` (30-day window) after capture.

## Common Utilities

- `load_daily_index_data.py`: refreshes global-index OHLC/context. Supports
  `--mode us-eur` (3 AM IST cron) and `--mode asia-partial` (9 AM IST cron).
- `calculate_underlying_features.py`: rebuilds underlying feature rows.
- `calculate_option_snapshot_calc.py`: computes option Greeks/IV fields.
- `analyze_precision_misses.py`: exports precision and recall miss CSVs with
  D-2/D-1/D/D+1/D+2 signal-feature context.
- `export_db_to_excel.py`: exports selected DB data for inspection.

## Backfills

`scripts/backfill_NIFTY/` contains historical loaders for NIFTY underlying,
option OHLC, option snapshots, volume, India VIX, news sentiment, and GIFT NIFTY
9:15 AM snapshots (`backfill_gift_nifty.py`).

## Notes

- Production jobs assume `.env` and Supabase connectivity.
- Render/cron commands should use repository-relative paths.
- Generated CSVs and logs belong under `output/`.
