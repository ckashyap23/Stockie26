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

### Cron schedule (Render)

| Time (IST) | Command | Notes |
|---|---|---|
| 3:00 AM | `python scripts/Common/load_daily_index_data.py --mode us-eur` | D-1 US/EUR OHLC |
| 9:00 AM | `python scripts/Common/load_daily_index_data.py --mode asia-partial` | D Asia partial OHLC |
| **9:20 AM** | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode open` | Saves gift_920 to GiftNiftySnapshot |
| **9:22 AM** | `python scripts/daily_NIFTY/daily_open_gap.py` | Reads gift_920 + gift_1515(D-1) from DB; writes open-gap features to SignalFeatureDaily for D-1 |
| **9:24 AM** | `python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1` | Prediction (reads gap features) + option selection; must run after open_gap |
| **9:28 AM** | `python scripts/daily_NIFTY/daily_paper_entry.py --underlying NIFTY --max-stale-seconds 300` | Paper trade entry using live Kite quotes; must run after nifty_signal |
| 3:15 PM | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode close` | Saves gift_1515 to GiftNiftySnapshot (used as D-1 reference tomorrow morning) |
| 3:47 PM | `python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY` | EOD OHLC; chains prediction + actual_trade_label |
| 4:00 PM | `python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY` | Option OHLC; chains option-selection + PnL backtest |

> **Morning ordering is critical**: open-gap features for signal_date D-1 are
> computed from today's 9:15 candle (closes at 9:20). The prediction at 9:22 reads
> those features. Paper entry at 9:28 consumes the resulting option selection.

`daily_market_refresh.py` automatically chains `daily_nifty_prediction.py` (5-day lookback)
after it completes. `daily_NIFTYoption_OHLC.py` automatically chains
`pipeline_upsert_option_selections.py` and `pipeline_backtest_pnl.py` (30-day window).

For individual script entry points, see `scripts/README.md`.

## Validation

Run targeted tests around the area changed. Common suites:

```powershell
python -m pytest tests/test_strategy_families.py tests/test_watch_promotion.py
python -m pytest tests/test_flask_ui_prediction_columns.py
python -m pytest tests/test_analyze_precision_misses.py
```
