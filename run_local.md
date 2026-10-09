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

- **Research Backtest**: strategy-grid NIFTY prediction evaluation and research
  artifacts.
- **Production Backtest**: production NIFTY predictions, realized movement
  metrics, and miss analysis.
- **Trades**: executed paper/live trades and replay.

## Daily Production Flow

Run upstream refresh jobs first, then the signal wrapper:

```powershell
python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY
python scripts/Common/load_daily_index_data.py --mode us-eur
python scripts/daily_NIFTY/daily_macro_refresh.py
python scripts/daily_NIFTY/daily_optionInstrument_refresh.py --underlying NIFTY
python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py
python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY
python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1
```

## Cron Schedule

| Time IST | Command | Notes |
|---|---|---|
| Before market, when expired | `python scripts/daily_NIFTY/daily_get_kite_access_token.py` | Interactive Kite token refresh; validate before unattended jobs |
| 3:00 AM | `python scripts/Common/load_daily_index_data.py --mode us-eur` | D-1 US/EUR OHLC |
| 9:00 AM | `python scripts/Common/load_daily_index_data.py --mode asia-partial` | D Asia partial OHLC |
| 3:00 PM | `python scripts/Common/load_daily_index_data.py --mode asia-final` | Stores final Asian OHLC for use as the next session's bounded overnight anchor |
| 9:00 AM | `python scripts/daily_NIFTY/daily_macro_refresh.py` | Stores D-1 macro closes, live provisional cash flows, and participant-wise FII index-futures OI against signal date D-1 before the D open |
| 9:05 AM | `python scripts/daily_NIFTY/daily_optionInstrument_refresh.py --type INDEX --underlying NIFTY` | Replaces/updates the current contract master used by paper trading |
| 9:20 AM | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode open` | Saves morning GIFT NIFTY reference |
| 9:22 AM | `python scripts/daily_NIFTY/daily_open_gap.py` | Writes open-gap features to `SignalFeatureDaily` |
| 9:24 AM | `python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1` | Prediction, guard layer, and option selection |
| 9:27 AM and as needed intraday | `python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py` | Captures current option quotes and Greeks inputs for forward paper execution |
| 9:28 AM | `python scripts/daily_NIFTY/daily_paper_entry.py --underlying NIFTY --max-stale-seconds 300` | Marketable-limit paper entry; records fill, NIFTY level, and delta; creates target/stop OCO |
| Every minute after entry | `python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY --max-stale-seconds 300` | Monitors open paper OCO orders; no 15:15 or maximum-holding-period exit |
| 3:15 PM | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode close` | Saves closing GIFT NIFTY reference |
| 3:47 PM | `python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY` | EOD OHLC; chains prediction/labels |
| 4:00 PM | `python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY` | Captures option OHLC for execution auditing |
| 4:10 PM | `python scripts/daily_NIFTY/daily_signal_summary.py` | Writes the daily prediction summary |
| 4:15 PM | `python scripts/daily_NIFTY/daily_paper_report.py` | Writes the paper-execution report |

Morning ordering is critical: open-gap features for signal_date D-1 are computed
from today's 9:15 candle after it closes at 9:20. The 9:24 prediction reads
those features. Paper entry at 9:28 consumes the resulting option selection.
Research and production backtests evaluate NIFTY predictions only; they do not
simulate option trades or PnL. Paper positions have no time exit. Their target
touches advance the anchored cascade, while a stop touch closes the position.
Historical option instruments, snapshots, candles, and calculations are not
required by either backtest; retain only the freshly refreshed current contract
master and forward paper-trading observations.

## Validation

Run targeted tests around the area changed. Common suites:

```powershell
python -m pytest tests/test_strategy_registry.py tests/test_execution_cascade.py
python -m pytest tests/test_flask_ui_prediction_columns.py
python -m pytest tests/test_analyze_precision_misses.py
```

For a full check:

```powershell
python -m pytest
```
