# Run Backtesting

Three distinct backtest types. Full details in [backtest/README.md](../backtest/README.md).

`UNDERLYING_LOOKBACK_DAYS` controls the forward window for NIFTY labels and signal quality scoring;
`TRADE_HORIZON_DAYS` independently controls option-position replay windows.

All percentage environment values are decimals: `0.01` means 1%, not 1%.

---

## Type 1 — Production Pipeline

Validates the cascade prediction → option selection → simulated PnL chain. Each step upserts into Supabase so the DB is the durable record.

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

---

## Type 2 — Research Strategy Grid

Tests all cascade strategy variants using current ITM delta selection and option snapshot replay.
Bypasses the precision floor — evaluates raw signal edge. See [backtest/vectorbt_research/README.md](../backtest/vectorbt_research/README.md).

```powershell
# All variants, full history from April 2025
python -m backtest.vectorbt_research.strategy_grid --start 2026-01-01

# Single month
python -m backtest.vectorbt_research.strategy_grid --start 2026-06-01 --end 2026-06-30

# Filter to specific strategies by name substring
python -m backtest.vectorbt_research.strategy_grid --variants Momentum,CalmTrend

# With stop-loss
python -m backtest.vectorbt_research.strategy_grid --start 2025-04-01 --stop-loss-pct 0.015
```

Outputs under `output/backtest/NIFTY/vectorbt_research/`:
- `strategy_grid_leaderboard.csv` — ranked by total PnL per unit
- `strategy_grid_trades.csv` — every trade with entry/exit/PnL
- `strategy_grid_summary.txt` — plain-text leaderboard

### VectorBT Research UI fields
| `Target pct(s)` | Option-premium profit target override used by the research replay. Current choices are 1%, 2%, 5%, 7%, and 10%. Multiple selections run a grid. |
| `Stop loss pct(s)` | Option-premium stop override. Current choices are no stop, 5%, 10%, 15%, 20%, and 30%. Multiple selections run a grid. It is independent of `STRESS_SL_PCT` and `CALM_SL_PCT`. |
| `Variant filter` | Runs all strategies or only selected research variants. This does not change the production promoted roster. |

If neither target nor stop is touched, replay exits at the end of the configured `TRADE_HORIZON_DAYS` window.

---

## Type 3 — Executed Trades (Paper/Live)

Evaluates actual fills from `PaperTradeResult`. No simulation — real entry/exit prices.
See [backtest/README.md → Type 3](../backtest/README.md#type-3--executed-trades-backtesting).

```powershell
# All paper trades on record
python -m backtest.vectorbt_trades.cli

# Filter by execution date range
python -m backtest.vectorbt_trades.cli --start 2026-07-01 --end 2026-07-31

```

Outputs under `output/backtest/NIFTY/vectorbt/`:
- `vectorbt_trades.csv` — trade-level PnL with exit reasons
- `vectorbt_summary.txt` — actual lot-based PnL + portfolio metrics

### Trades UI fields
- `Replay start` / `Replay end` filter by `paper_trade_date`, the date the option position was actually entered. The UI defaults to 2026-06-01 through today.
