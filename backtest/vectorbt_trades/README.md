# Stockie VectorBT — Execution Replay (Type 3 Backtesting)

Replays actual executed paper/live trades through VectorBT for portfolio analytics.

This document describes Type 3 only. Signal-family, D0/D1/D2 watch promotion,
production option selection, and miss-analysis behavior belong to the production
workflow documented in [`TeamDocs/RunBacktesting.md`](../../TeamDocs/RunBacktesting.md).

Source of truth: `PaperTradeResult` + `PaperExecutionSignal` in Supabase. Actual
fill prices, exit reasons, timestamps, and recorded charges are historical facts.
VectorBT trade replay does not rewrite those facts.

## Current-policy overlay

Replay displays target/stop percentages and prices recalculated from the actual
entry fill using the trade regime and current `CALM_*` / `STRESS_*` env values.
This is a visibility overlay only for already executed trades.

Replay quantity and total PnL are scenario-sized from the actual entry fill using
`PAPER_TRADING_CAPITAL` and `PAPER_CAPITAL_PER_TRADE_PCT`. The persisted paper
execution rows themselves are not modified.

Recorded historical exits and PnL are never rewritten by a replay. Where no
execution exists, production replay may use sparse option snapshots as an
approximation; such snapshots cannot reconstruct the full intraday path.

## What this answers

> "How did our actual executed paper trades perform as a portfolio?"

Distinct from the other two backtest types:

| Type | Source | Question |
|---|---|---|
| `backtest/production/` | `NiftyPrediction` + `NiftyOptionSelection` | What would the production pipeline have predicted, selected, and replayed? |
| `backtest/vectorbt_research/` | NIFTY feature store → strategy signals | Which research strategies have edge? |
| `backtest/vectorbt_trades/` | `PaperTradeResult` actual fills | How did executed trades perform? |

## Run

```powershell
# All paper trades on record
python -m backtest.vectorbt_trades.cli

# Filter by paper_trade_date range
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --end 2026-06-30

# With extra scenario fees/slippage applied on top of fills
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --fees 0.0003 --slippage 0.0005

# MODE env variable is used if --mode is not passed
python -m backtest.vectorbt_trades.cli --start 2026-06-01
```

## Outputs

Written to `output/backtest/NIFTY/vectorbt/`:

| File | Contents |
|---|---|
| `paper_executed_trades.csv` | All loaded trades (`CLOSED` + `OPEN`) |
| `paper_closed_trades.csv` | Closed trades used in the VectorBT replay |
| `paper_open_trades.csv` | Open positions, entered but not yet closed |
| `vectorbt_trades.csv` | Trade-level replay output with actual entry/exit prices, policy target/SL overlay, scenario quantity, gross PnL, and net PnL |
| `vectorbt_summary.txt` | Actual lot-based PnL from DB plus VectorBT portfolio metrics |

## PnL: two numbers, two meanings

Authoritative actual PnL comes from `PaperTradeResult`, computed from actual fill
prices and stored charges at execution time.

Scenario replay PnL is derived from the actual entry fill, actual exit fill,
exchange lot size, `PAPER_TRADING_CAPITAL`, and `PAPER_CAPITAL_PER_TRADE_PCT`.
It answers "what would the same executed trade have looked like at the current
paper capital allocation?"

VectorBT portfolio metrics are computed from a synthetic two-point price series:

```text
entry_time -> entry_price
exit_time  -> exit_price
```

No option snapshot prices are needed. No exit rule simulation is performed. The
paper monitor scripts already evaluated exits in real time.

## Open trades

Open positions are loaded and written to `paper_open_trades.csv` but excluded
from the closed-trade VectorBT replay because no exit price exists. Their MTM PnL
is visible in `daily_paper_report.py`, which reads live/current trade state.

## When to run

After market close, once `daily_paper_monitor.py` has run its final cycle
(at or after 15:15 IST). The monitor's time-exit close ensures same-day trades
are closed before EOD.

For multi-day positions, run after the final exit cycle for those trades.

## Live mode

`--mode live` raises `NotImplementedError` until live execution tables are
implemented. Switch to live trading by implementing parallel execution tables and
pointing this adapter at them.
