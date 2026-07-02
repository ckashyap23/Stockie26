# Scripts

Only executable source scripts belong here. Generated `__pycache__`, local
output, and one-off scratch scripts must not be committed.

Active NIFTY pipeline scripts are split into daily jobs, backfill jobs, and shared common jobs.

## Daily NIFTY

- `daily_NIFTY/daily_get_kite_access_token.py` - refresh Kite access token.
  - `python scripts/daily_NIFTY/daily_get_kite_access_token.py`
- `daily_NIFTY/daily_market_refresh.py` - fetch daily underlying OHLC and update `SignalFeatureDaily`.
  - `python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY`
  - `python scripts/daily_NIFTY/daily_market_refresh.py --underlying ALL`
- `daily_NIFTY/daily_optionInstrument_refresh.py` - refresh active NIFTY option instruments.
  - `python scripts/daily_NIFTY/daily_optionInstrument_refresh.py --underlying NIFTY`
- `daily_NIFTY/daily_NIFTYoption_snapshot.py` - fetch live NIFTY option snapshots and calculate greeks.
  - `python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py`
- `daily_NIFTY/daily_NIFTYoption_OHLC.py` - fetch live Kite quote OHLC for active NIFTY options into `OptionOhlc` after market close.
  - `python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY`
- `daily_NIFTY/daily_news_sentiment.py` - generate pre-market news sentiment for research and persist article/market sentiment rows. Not consumed by production prediction yet.
  - `python scripts/daily_NIFTY/daily_news_sentiment.py --sector-classifier keyword`
  - Render/lightweight hosted FinBERT: set `NEWS_SENTIMENT_SCORER=hf_finbert` and `HF_TOKEN`, then run `python scripts/daily_NIFTY/daily_news_sentiment.py --sector-classifier llm --no-transformers`
- `daily_NIFTY/daily_nifty_prediction.py` - run the production cascade prediction and persist `NiftyPrediction` rows.
  - `python scripts/daily_NIFTY/daily_nifty_prediction.py`
- `daily_NIFTY/daily_option_selection.py` - select the NIFTY option for a persisted prediction and persist `NiftyOptionSelection`.
  - `python scripts/daily_NIFTY/daily_option_selection.py --trade-date 2026-06-25 --model-version cascade_v1`
- `daily_NIFTY/daily_nifty_signal.py` - production cron wrapper after upstream market/global/option refresh jobs; runs prediction, option selection, persists both DB rows, and prints one selected option trade plan as JSON.
  - `python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1`
  - `python scripts/daily_NIFTY/daily_nifty_signal.py --skip-prediction --trade-date 2026-06-25 --model-version cascade_v1`
- `daily_NIFTY/daily_paper_entry.py` - create due Stockie paper execution signals from `NiftyOptionSelection`, then open planned paper trades using live Kite option quotes.
  - `python scripts/daily_NIFTY/daily_paper_entry.py --trade-date 2026-06-29`
- `daily_NIFTY/daily_paper_monitor.py` - update MTM and apply stop loss, target 2, target 1, then final-session close priority; target touches use ratchet logic.
  - `python scripts/daily_NIFTY/daily_paper_monitor.py --trade-date 2026-06-29`
  - `python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY`
  - `--max-open-days` overrides `TRADE_HORIZON_DAYS`; entry counts as day 1.
- `daily_NIFTY/daily_paper_report.py` - export read-only paper trade CSV and gross/charges/net summary.
  - `python scripts/daily_NIFTY/daily_paper_report.py --trade-date 2026-06-29`
- `daily_NIFTY/refresh_nifty50_sector_weights.py` - refresh NSE NIFTY50 sector weights for news sentiment weighting.
  - `python scripts/daily_NIFTY/refresh_nifty50_sector_weights.py`

## Backfill NIFTY

- `backfill_NIFTY/backfill_underlying.py` - backfill underlying OHLC and update `SignalFeatureDaily`.
  - `python scripts/backfill_NIFTY/backfill_underlying.py --underlying NIFTY --start 2026-01-01 --end 2026-06-30`
- `backfill_NIFTY/backfill_NIFTYoptions_from_historical.py` - backfill NIFTY option snapshots from Kite historical candles and calculate greeks.
  - `python scripts/backfill_NIFTY/backfill_NIFTYoptions_from_historical.py --underlying NIFTY --start 2026-01-01 --end 2026-06-30`
- `backfill_NIFTY/backfill_NIFTYoptions_OHLC.py` - backfill daily-grain NIFTY option OHLC into `OptionOhlc` from Kite historical daily candles.
  - `python scripts/backfill_NIFTY/backfill_NIFTYoptions_OHLC.py --from-date 2026-04-01 --to-date 2026-06-30 --underlying NIFTY`
- `backfill_NIFTY/backfill_nifty_volume.py` - backfill NIFTY near-month futures volume from NSE FO bhavcopy into `UnderlyingSnapshot` and recompute `SignalFeatureDaily` volume windows.
  - `python scripts/backfill_NIFTY/backfill_nifty_volume.py --start 2026-01-01 --end 2026-06-30`
- `backfill_NIFTY/backfill_india_vix.py` - backfill India VIX into `MacroFactorDaily`.
  - `python scripts/backfill_NIFTY/backfill_india_vix.py --start 2025-01-01 --end 2026-06-30`
- `backfill_NIFTY/backfill_news_sentiment.py` - batch historical news sentiment generation by target date.
  - `python scripts/backfill_NIFTY/backfill_news_sentiment.py --start-date 2026-06-01 --end-date 2026-06-24 --sector-classifier keyword`
  - Hosted FinBERT: set `NEWS_SENTIMENT_SCORER=hf_finbert` and `HF_TOKEN`, then add `--no-transformers`.

## Common

- `Common/calculate_underlying_features.py` - write underlying technical features to `SignalFeatureDaily`.
  - `python scripts/Common/calculate_underlying_features.py --underlying NIFTY --start 2026-01-01 --end 2026-06-30`
- `Common/calculate_option_snapshot_calc.py` - calculate IV/greeks into `OptionSnapshotCalc`.
  - `python scripts/Common/calculate_option_snapshot_calc.py --from-date 2026-01-01 --to-date 2026-06-30`
- `Common/load_daily_index_data.py` - fetch global index OHLC rows and persist `GlobalIndexOhlc`.
  - `python scripts/Common/load_daily_index_data.py --no-local-output`
- `Common/build_trading_calendar.py` - populate `TradingCalendar` so predictions and option replays use the next valid NSE session instead of raw snapshot dates.
  - `pip install -r requirements-calendar.txt`
  - `python scripts/Common/build_trading_calendar.py --start 2026-01-01 --end 2026-12-31`
  - `python scripts/Common/build_trading_calendar.py --start 2026-06-01 --end 2026-06-30 --validate-with-kite`
- `Common/download_finbert_model.py` - download FinBERT locally for local fallback when hosted HF inference fails.
  - `python scripts/Common/download_finbert_model.py --output-dir models/ProsusAI/finbert`
- `Common/export_db_to_excel.py` - export NIFTY underlying and option snapshot/greeks to Excel.
  - `python scripts/Common/export_db_to_excel.py` - defaults to 2026-04-01 to today, output in `output/db/`
  - `python scripts/Common/export_db_to_excel.py --start 2026-04-01 --end 2026-06-30 --snapshot-label close`

## Render Cron Notes

Render runs from the repository root on Linux, so script paths and casing must
match exactly. Use these commands for cron jobs:

```bash
python scripts/Common/load_daily_index_data.py --no-local-output
python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY
```

For the option OHLC daily job, schedule around 15:40 to 15:45 IST after market
close, which is 10:10 to 10:15 UTC on Render. Required environment variables
are `DATABASE_PROVIDER`, `SUPABASE_CONN_STR`, `KITE_API_KEY`,
`KITE_API_SECRET`, and a valid Kite access token source.
