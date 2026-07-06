# Stockie VectorBT â€” Execution Replay (Type 3 Backtesting)

Replays **actual executed paper/live trades** through VectorBT for portfolio analytics.

This document describes Type 3 only. Signal-family, D0/D1/D2 watch promotion,
and miss-analysis behavior belong to the production workflow documented in
[`TeamDocs/RunBacktesting.md`](../../TeamDocs/RunBacktesting.md).

Paper fill charges are requested from Kite and stored with entry/exit orders.
Replay reports gross P&L, stored charges, and net P&L. CLI fees/slippage are
optional additional scenario assumptions.

**Source of truth:** `PaperTradeResult` + `PaperExecutionSignal` tables in Supabase â€” actual fill prices, actual exit reasons (STOP_LOSS_HIT, TARGET_1_HIT, TIME_EXIT, MAX_DAYS_EXIT), actual lot sizes.

This is **not** a signal simulation. No option snapshot lookups, no exit rule re-evaluation. The trades already happened; this layer adds equity curve, drawdown, and Sharpe analytics on top.

---

## What this answers

> "How did our actual executed paper trades perform as a portfolio?"

Distinct from the other two backtest types:

| Type | Source | Question |
|---|---|---|
| `backtest/production/` | `NiftyPrediction` + `NiftyOptionSelection` | What would the pipeline have predicted/selected? |
| `backtest/vectorbt_research/` | NIFTY feature store â†’ strategy signals | Which research strategies have edge? |
| **`backtest/vectorbt_trades/`** | **`PaperTradeResult` (actual fills)** | **How did our executed trades perform?** |

---

## Run

```powershell
# All paper trades on record
python -m backtest.vectorbt_trades.cli

# Filter by paper_trade_date range
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --end 2026-06-30

# With actual fee/slippage applied on top of fills
python -m backtest.vectorbt_trades.cli --start 2026-06-01 --fees 0.0003 --slippage 0.0005

# MODE env variable is used if --mode not passed
python -m backtest.vectorbt_trades.cli --start 2026-06-01
```

---

## Outputs

Written to `output/backtest/NIFTY/vectorbt/`:

| File | Contents |
|---|---|
| `paper_executed_trades.csv` | All loaded trades (CLOSED + OPEN) |
| `paper_closed_trades.csv` | CLOSED trades used in the vectorbt replay |
| `paper_open_trades.csv` | OPEN positions (entered but not yet closed) |
| `vectorbt_trades.csv` | Trade-level output enriched with actual pnl_per_lot, exit_reason |
| `vectorbt_summary.txt` | Two sections: actual PnL (lot-based, from DB) + vectorbt portfolio metrics |

---

## PnL: two numbers, two meanings

**Actual PnL** (authoritative): `pnl_per_lot` column comes directly from `PaperTradeResult`, computed from actual fill prices at execution time.

**VectorBT metrics**: computed from a synthetic 2-point price series (entry_time/price â†’ exit_time/price), sized by `initial_cash / entry_price` â€” normalized positioning, not actual lot sizes. Valid for equity curve shape, drawdown %, win rate, Sharpe.

The summary file shows both clearly.

---

## How the replay works

For each CLOSED trade, a 2-point price series is built:
```
entry_time â†’ entry_price   (actual Kite fill at open)
exit_time  â†’ exit_price    (actual fill at stop/target/time exit)
```

VectorBT runs `Portfolio.from_signals` over this synthetic series. If vectorbt is not installed, a pandas fallback computes metrics directly from fills.

No option snapshot prices are needed. No exit rule simulation. The monitor scripts (`daily_paper_monitor.py`, running every 15 min) already evaluated exits in real time.

---

## OPEN trades

OPEN positions (entered but not yet closed) are loaded and written to `paper_open_trades.csv` but are **excluded from the vectorbt replay** since no exit price exists. Their MTM PnL is visible in `daily_paper_report.py` which reads live from `PaperTradeResult.current_price`.

---

## When to run

After market close, once `daily_paper_monitor.py` has run its final cycle (â‰¥15:15 IST). The monitor's TIME_EXIT close at 15:15 ensures all same-day trades are closed before EOD.

For multi-day positions (`MAX_DAYS_EXIT`), run after the final exit cycle for those trades.

---

## Live mode

`--mode live` will raise `NotImplementedError` until live execution tables (`LiveTradeResult` etc.) are implemented. Switch to live trading by implementing a parallel set of execution tables and pointing this adapter at them.

