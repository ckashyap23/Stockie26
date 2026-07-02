# Run Backtesting

Three distinct backtest types. Full details in [backtest/README.md](../backtest/README.md).

`UNDERLYING_LOOKBACK_DAYS` controls NIFTY labels and signal quality;
`TRADE_HORIZON_DAYS` independently controls option-position replay windows.

---

## Type 1 — Production Pipeline

Validates the cascade prediction → option selection → simulated PnL chain.
Each step upserts into Supabase so the DB is the durable record.

```powershell
# Step 1 — regenerate cascade predictions for all history and upsert to NiftyPrediction
python scripts/daily_NIFTY/daily_nifty_prediction.py

# Step 2 — run option selection for every CALL/PUT signal date and upsert to NiftyOptionSelection
python backtest/production/pipeline_upsert_option_selections.py
# Optional: restrict to a date window
python backtest/production/pipeline_upsert_option_selections.py --start 2026-04-01

# Step 3 — simulate PnL from production signals (date range optional)
python backtest/production/pipeline_backtest_pnl.py --start 2025-01-01
python backtest/production/pipeline_backtest_pnl.py --start 2026-06-01 --end 2026-06-30
```

Outputs under `output/backtest/NIFTY/production/`:
- `NIFTY_prediction_summary.txt` — in-sample + walk-forward accuracy, signal quality
- `production_pnl_trades.csv` — trade-by-trade simulated PnL
- `production_pnl_summary.txt` — win rate, total PnL, exit breakdown

All prediction and option-selection data lives in Supabase (`NiftyPrediction`, `NiftyOptionSelection`).
No intermediate CSVs are written — DB is the durable store.

---

## Type 2 â€” Research Strategy Grid

Tests all cascade strategy variants using current ITM delta selection and option
snapshot replay.
Bypasses the precision floor â€” evaluates raw signal edge. See [backtest/vectorbt_research/README.md](../backtest/vectorbt_research/README.md).

```powershell
# All variants, full history from April 2025
python -m backtest.vectorbt_research.strategy_grid --start 2025-04-01

# Single month
python -m backtest.vectorbt_research.strategy_grid --start 2026-06-01 --end 2026-06-30

# Filter to specific strategies by name substring
python -m backtest.vectorbt_research.strategy_grid --variants Momentum,CalmTrend

# With stop-loss
python -m backtest.vectorbt_research.strategy_grid --start 2025-04-01 --stop-loss-pct 0.015
```

Outputs under `output/backtest/NIFTY/vectorbt_research/`:
- `strategy_grid_leaderboard.csv` â€” ranked by total PnL per unit
- `strategy_grid_trades.csv` â€” every trade with entry/exit/PnL
- `strategy_grid_summary.txt` â€” plain-text leaderboard

---

## Type 3 â€” Executed Trades (Paper/Live)

Evaluates actual fills from `PaperTradeResult`. No simulation â€” real entry/exit prices.
See [backtest/README.md â†’ Type 3](../backtest/README.md#type-3--executed-trades-backtesting).

```powershell
# All paper trades on record
python -m backtest.vectorbt_trades.cli

# Filter by execution date range
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --end 2026-06-30

# With fees and slippage on top of fills
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --fees 0.0003 --slippage 0.0005
```

Outputs under `output/backtest/NIFTY/vectorbt/`:
- `vectorbt_trades.csv` â€” trade-level PnL with exit reasons
- `vectorbt_summary.txt` â€” actual lot-based PnL + portfolio metrics

