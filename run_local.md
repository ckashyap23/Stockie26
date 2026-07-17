# Run Locally

This repo is DB-first. Local files under `output/` are developer artifacts; the
durable production records live in Supabase.

## Prerequisites

- Python environment activated.
- `.env` present at repo root.
- `SUPABASE_CONN_STR` available.
- Kite credentials/token available for market, option, and paper-trading jobs.

## Dashboard

```powershell
python flask_app.py
```

Open `http://127.0.0.1:5000`.

The dashboard tabs are:

- **Research**: VectorBT strategy-grid runs and research artifacts.
- **Production Strategies**: production predictions, option selection, PnL, and
  miss analysis. The default window is `2024-01-01` through today.
- **Trades**: executed paper/live trades and replay.

## Daily Production Flow

Run upstream refresh jobs first, then the signal wrapper:

```powershell
python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY
python scripts/Common/load_daily_index_data.py --mode us-eur
python scripts/daily_NIFTY/daily_optionInstrument_refresh.py --underlying NIFTY
python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py
python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY
python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1
```

### Cron modes for global index data

```powershell
# 3 AM IST — US/EUR complete 1d OHLC for yesterday
python scripts/Common/load_daily_index_data.py --mode us-eur

# 9 AM IST — Asia partial 5m OHLC for today (open to 9:20 AM IST)
python scripts/Common/load_daily_index_data.py --mode asia-partial

For individual script entry points, see `scripts/README.md`.

## Validation

Run targeted tests around the area changed. Common suites:

```powershell
python -m pytest tests/test_strategy_families.py tests/test_watch_promotion.py
python -m pytest tests/test_flask_ui_prediction_columns.py
python -m pytest tests/test_analyze_precision_misses.py
```
