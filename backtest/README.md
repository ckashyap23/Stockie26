# Backtest

Three distinct backtesting types live here. They share the same underlying data
(`OptionSnapshot`, `NiftyPrediction`, `NiftyOptionSelection` in Supabase) but
serve different questions.

Current configuration: `UNDERLYING_LOOKBACK_DAYS` drives NIFTY labels and signal
quality; `TRADE_HORIZON_DAYS` drives option holding. Production summaries use
2025-01-01 through today. Research selects ITM options by delta.

---

## Type 1 — Production Pipeline Backtesting

**Question:** What would the production cascade pipeline have predicted and
selected historically, if run on past dates?

**Files:** `backtest/production/`
- `pipeline_upsert_option_selections.py` — batch-upserts `NiftyOptionSelection` for all CALL/PUT signal dates
- `pipeline_backtest_pnl.py` — simulates PnL by replaying intraday `OptionSnapshot` prices
- `pipeline_backtest_optionselection.py` — legacy research tool (reads CSV or DB, prints summary; does NOT upsert to DB)

**Run order (DB-first — all data persists to Supabase):**

```powershell
# Step 1 — regenerate cascade predictions and upsert to NiftyPrediction
python scripts/daily_NIFTY/daily_nifty_prediction.py

# Step 2 — run option selection for every CALL/PUT date and upsert to NiftyOptionSelection
python backtest/production/pipeline_upsert_option_selections.py
# Optional: restrict to a date window
python backtest/production/pipeline_upsert_option_selections.py --start 2026-04-01

# Step 3 — simulate PnL from production signals using intraday option snapshots
python backtest/production/pipeline_backtest_pnl.py --start 2025-01-01
python backtest/production/pipeline_backtest_pnl.py --start 2026-06-01 --end 2026-06-30
```

**Outputs:** `output/backtest/NIFTY/production/`
```
NIFTY_prediction_summary.txt       — in-sample + walk-forward accuracy, signal quality  (Step 1)
production_signals.csv             — all loaded NiftyOptionSelection rows                (Step 3)
production_signals_no_snapshot.csv — signals with no intraday snapshot data              (Step 3)
production_pnl_trades.csv          — trade-by-trade simulated PnL                       (Step 3)
production_pnl_summary.txt         — win rate, total PnL, exit breakdown                (Step 3)
```

Prediction and option-selection data live in Supabase (`NiftyPrediction`,
`NiftyOptionSelection`) — no intermediate CSVs are written.

**What this validates:** That the production cascade (regime routing, precision
floor, strategy voting) plus the option selection pipeline (candidate filtering,
scoring, risk rules) produce sane historical outputs. Step 3 adds simulated PnL
by replaying intraday OptionSnapshot prices against the pipeline's own targets
and stop levels — closest simulation of what runs in production daily.

---

## Type 2 â€” Research Pipeline Backtesting

**Question:** How do different prediction signal strategies perform when their
signals are mapped directly to ATM option trades and replayed intraday?

**File:** `backtest/vectorbt_research/strategy_grid.py`

**Coverage:**
- **Promoted strategies** â€” the same signals used in production (`MomentumDirectional_CallExpGuard`,
  `OversoldBounceCall_ContextRoom`, `DownMomentumPut_MoreTrades`, etc.)
- **Experimental strategies** â€” cascade signals not yet in the production roster
  (`MAAlignmentRoom`, `RangeBreakout`, `CalmTrendCall`, `CalmFadePut`,
  `CalmMomentumPut`, additional OversoldBounce/DownMomentum variants)
- **Parametric sweeps** â€” simple threshold variants (`RsiReversion`, `MaSpread`,
  `MAAlignmentRoom_Fast`) for quick signal shape exploration
- **Regime routing** â€” all signals receive a `regime` column (calm/stress,
  same thresholds as production); calm-only strategies are suppressed on stress
  dates and vice versa

**Run:**

```powershell
# All variants, from April 2025 to today
python -m backtest.vectorbt_research.strategy_grid --start 2025-04-01

# Filter to specific variants by name substring (case-insensitive)
python -m backtest.vectorbt_research.strategy_grid --variants Momentum,CalmTrend

# With stop-loss
python -m backtest.vectorbt_research.strategy_grid --start 2025-04-01 --stop-loss-pct 0.015

# See all options
python -m backtest.vectorbt_research.strategy_grid --help
```

**Outputs:** `output/backtest/NIFTY/vectorbt_research/`
```
strategy_grid_leaderboard.csv    â€” ranked by total PnL per unit
strategy_grid_trades.csv         â€” every trade with entry/exit/PnL
strategy_grid_trade_plans.csv    â€” which ATM option was selected per signal
strategy_grid_definitions.csv    â€” name + description for every variant run
strategy_grid_summary.txt        â€” plain-text leaderboard
```

**Key distinction from Type 1:** This bypasses the precision floor and
walk-forward eligibility gates. It evaluates raw signal shape â†’ ATM option
performance, not the full cascade's gated output. Use it to find whether a
signal has any edge before investing in full production evaluation.

See `backtest/vectorbt_research/README.md` for how to add new strategy variants.

---

## Type 3 â€” Executed Trades Backtesting

**Question:** How did our actual executed paper/live trades perform as a portfolio?

**File:** `backtest/vectorbt_trades/cli.py`

This reads actual fills from `PaperTradeResult` + `PaperExecutionSignal` â€” the
real entry prices, exit prices, exit reasons (STOP_LOSS_HIT, TARGET_1_HIT,
TIME_EXIT, MAX_DAYS_EXIT) recorded by the monitor scripts. No exit simulation,
no option snapshot lookups. VectorBT adds equity curve, drawdown, and Sharpe
analytics on top of the actual fills.

**Run:**

```powershell
# All paper trades on record
python -m backtest.vectorbt_trades.cli

# Filter by execution date range (paper_trade_date)
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --end 2026-06-30

# MODE env variable used if --mode is not passed (currently MODE=paper in .env)
python -m backtest.vectorbt_trades.cli --start 2026-06-01

# With fees and slippage applied on top of fills
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --fees 0.0003 --slippage 0.0005
```

**Outputs:** `output/backtest/NIFTY/vectorbt/`
```
paper_executed_trades.csv    â€” all loaded trades (CLOSED + OPEN)
paper_closed_trades.csv      â€” CLOSED trades used in vectorbt replay
paper_open_trades.csv        â€” OPEN positions (entered, not yet closed)
vectorbt_trades.csv          â€” trade-level output with pnl_per_lot, exit_reason
vectorbt_summary.txt         â€” actual PnL (lot-based) + vectorbt portfolio metrics
```

**Key distinction from Types 1 & 2:**
- Type 1 re-runs the *pipeline logic* on past dates to validate signal quality
- Type 2 tests *research signals* not yet in production
- **Type 3 evaluates the *actual trades we executed* â€” what really happened in paper/live mode**

The entry and exit prices come from the DB (paper fills), not from intraday
option snapshot simulation. Stored Kite charge calculations provide gross,
charges, and net P&L; optional fees/slippage are extra scenarios.

---

## Strategy Ownership

Production strategy logic lives in `src/technical_analysis/cascade/` and
`src/technical_analysis/optionselection/`. Backtest folders consume that logic
â€” they are not the source of truth for production strategy rules.


