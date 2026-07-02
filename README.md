# Stockie26

NIFTY options trading signal system — cascade prediction, option selection, and paper execution.

## Daily Signal Flow

The pipeline runs across three cron windows each trading day (`daily_cron_dispatcher.py`):

```mermaid
flowchart TD
    A["**D0 16:30 IST — Post-market (11:00 UTC)**\ndaily_market_refresh.py\n→ writes D0 UnderlyingOhlc, SignalFeatureDaily, VIX into DB\ndaily_NIFTYoption_OHLC.py → captures D0 option candles\ndaily_paper_monitor.py → closes any still-open positions\ndaily_paper_report.py → final PnL for D0 trades"]
    --> B

    B["**D1 08:00 IST — Pre-market (02:30 UTC)**\nAll global markets have closed overnight\n\n① load_daily_index_data.py\n   → refreshes GlobalIndexOhlc through D1 pre-open\n   (US close, Europe close, Asia close — overnight gap data now available)"]
    --> C

    C["② daily_nifty_prediction.py\n→ build_base() reads SignalFeatureDaily + GlobalIndexOhlc\n→ global index returns for D0 include overnight D0→D1 gap\n→ cascade → upsert NiftyPrediction\n[signal_date=D0, next_trade_date=D1, final_prediction, regime]"]
    --> D

    D["③ daily_nifty_signal.py\n→ fetch_prediction_row (by signal_date=D0 or next_trade_date=D1)\n→ run_option_selection() using D0 close_1515 + IV history\n→ upsert NiftyOptionSelection [trade_date=D1]"]
    --> E

    E["④ daily_paper_entry.py\n→ prepare_paper_signals(trade_date=D1)\n   copies NiftyOptionSelection → PaperExecutionSignal [PLANNED]\n→ enter_due_paper_trades(trade_date=D1)\n   gap gate: re-checks GlobalIndexOhlc moves as a safety net\n   enters at live Kite open quote → PaperExecutionSignal [ENTERED]"]
    --> F

    F["**D1 12:00 IST — Midday (06:30 UTC)**\ndaily_NIFTYoption_snapshot.py → mid-session IV snapshots\ndaily_paper_monitor.py → close on target/stop hit"]
```

## Date Semantics

| Column | Table | Meaning |
|---|---|---|
| `signal_date` | `NiftyPrediction` | D0 — NIFTY close being observed by the model |
| `next_trade_date` | `NiftyPrediction` | D1 — next NSE trading day (execution day) |
| `trade_date` | `NiftyOptionSelection` | D1 — day the option will be bought |
| `paper_trade_date` | `PaperExecutionSignal` | D1 — day the trade was physically entered |
| `signal_trade_date` | `PaperExecutionSignal` | D0 — original signal day (used for holiday gap gate) |
| `trade_date` | `GlobalIndexOhlc`, `UnderlyingOhlc`, `OptionOhlc` | Candle date for that market (unrelated to prediction date naming) |

**Gap gate note:** The global overnight gap (US/Europe/Asia D0→D1 moves) is loaded in step ① *before* prediction runs. By the time the cascade runs in step ②, `global_us_return_mean` etc. already incorporate the overnight move and influence the prediction itself. The gap gate check inside `daily_paper_entry.py` (step ④) is a secondary safety net for stale data or multi-day holiday gaps.

## Project Structure

| Folder | Purpose |
|---|---|
| `src/technical_analysis/cascade/` | Feature dataset assembly, cascade prediction engine |
| `src/technical_analysis/optionselection/` | Option strategy selection |
| `src/execution/` | Paper trade entry, monitoring, gap gate |
| `src/data_manager/db/` | Supabase/Postgres client, upsert helpers, migrations |
| `scripts/daily_NIFTY/` | Daily operational scripts (cron entry points) |
| `scripts/Common/` | Backfill and utility scripts |
| `backtest/` | Production PnL replay, vectorbt research |
| `output/backtest/NIFTY/production/` | Prediction CSV and summary outputs |
| `TeamDocs/` | Schema references and module documentation |
