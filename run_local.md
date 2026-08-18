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

Dashboard tabs:

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

## Cron Schedule

| Time IST | Command | Notes |
|---|---|---|
| 3:00 AM | `python scripts/Common/load_daily_index_data.py --mode us-eur` | D-1 US/EUR OHLC |
| 9:00 AM | `python scripts/Common/load_daily_index_data.py --mode asia-partial` | D Asia partial OHLC |
| 9:20 AM | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode open` | Saves morning GIFT NIFTY reference |
| 9:22 AM | `python scripts/daily_NIFTY/daily_open_gap.py` | Writes open-gap features to `SignalFeatureDaily` |
| 9:24 AM | `python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1` | Prediction, guard layer, and option selection |
| 9:28 AM | `python scripts/daily_NIFTY/daily_paper_entry.py --underlying NIFTY --max-stale-seconds 300` | Paper entry using live Kite quotes |
| 3:15 PM | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode close` | Saves closing GIFT NIFTY reference |
| 3:47 PM | `python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY` | EOD OHLC; chains prediction/labels |
| 4:00 PM | `python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY` | Option OHLC; chains option-selection and PnL replay |

Morning ordering is critical: open-gap features for signal_date D-1 are computed
from today's 9:15 candle after it closes at 9:20. The 9:24 prediction reads
those features. Paper entry at 9:28 consumes the resulting option selection.

## Validation

Run targeted tests around the area changed. Common suites:

```powershell
python -m pytest tests/test_strategy_families.py tests/test_execution_cascade.py
python -m pytest tests/test_flask_ui_prediction_columns.py
python -m pytest tests/test_analyze_precision_misses.py
```

For a full check:

```powershell
python -m pytest
```
