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
- `NIFTY_stress_in_sample_precision_misses.csv` — incorrect actionable fires
  with signal-day diagnostics
- `NIFTY_stress_in_sample_recall_misses.csv` — missed CALL/PUT days with
  near-miss diagnostics

Generate both miss reports with
`python scripts/Common/analyze_precision_misses.py`, or use **Analyze Misses**
beside the Production summary to generate and download both files.

All prediction and option-selection data lives in Supabase (`NiftyPrediction`, `NiftyOptionSelection`).

Production option exits use one ratcheting premium target plus a stop loss from
`STRESS_TARGET_PCT` / `CALM_TARGET_PCT` and `STRESS_SL_PCT` / `CALM_SL_PCT`.
When a target is reached, the exact prior target becomes the new cascade base and
the stop is recomputed with `STRESS_SL_DIVIDER` or `CALM_SL_DIVIDER`, capped by
global `N_CAP`. Live paper trading can apply this with frequent quotes.
Historical production PnL preserves actual paper-trade fills/exits where they
exist; otherwise it falls back to sparse option snapshots, so those simulated
rows are approximate and may differ from live execution.

### Production watch and promotion layer

The promotion layer is a stateful production-cascade overlay, not an individual
research-grid strategy. Its implementation is in
`src/technical_analysis/cascade/watch_promotion.py`.

Canonical family metadata and human-readable strategy definitions live in
`src/technical_analysis/strategy_families.yaml`. The registry separates three
strategy types:

- `RESEARCH` — scored independently in the research grid; never creates a
  production trade or watch. `MACD_EMA5_20` is research-only and may be used as
  confirmation metadata.
- `TRADE_ELIGIBLE` — participates in the normal precision cascade and
  can also create/confirm a watch when the hard cascade remains flat.
- `WATCH_ONLY` — excluded from the hard cascade; it becomes actionable
  only after D1/D2 confirmation from the same strategy family.

The complete strategy roster and authority are deliberately not duplicated in
this runbook. Use `src/technical_analysis/strategy_families.yaml` as the source
of truth for current `TRADE_ELIGIBLE`, `WATCH_ONLY`, and `RESEARCH` variants.

Prediction fields:

- `final_prediction` — original cascade output, retained only for audit.
- `watch_signal` — D0 `CALL_3D_WATCH` or `PUT_3D_WATCH` created when the cascade is
  `NO_POSITION` but existing production strategies fire on exactly one side.
- `prior_watch_signal` / `prior_watch_age` — active watch from one or two trading
  sessions earlier.
- `promoted_prediction` — hard CALL/PUT after same-direction confirmation.
- `promotion_reason` — creation, confirmation, veto, conflict, or expiry reason.
- `effective_prediction` — canonical signal used by the UI, precision/recall,
  option selection, production PnL, and paper trading. It equals
  `final_prediction` when the original cascade fired; otherwise it uses a confirmed
  `promoted_prediction`.

Lifecycle:

1. D0: a one-sided raw strategy fire creates a 3-session watch.
2. D1 or D2: a same-direction signal from the same strategy family, or favorable
   price action tied to the active WATCH_ONLY family, may promote the watch to hard
   CALL/PUT when there is no opposite conflict. A different family cannot promote it.
3. A promoted/expired watch is consumed. An unconfirmed watch expires after D2.

Signal-time promotion guards (D0-known features only):

- OversoldBounce CALL remains on watch when `volume_hybrid < 0.80` and
  `bb_width < 0.055`.
- A RangeBreakout CALL reaching D2 must receive a same-day RangeBreakout-family
  CALL confirmation; otherwise the watch expires.
- Range breakout/breakdown watches require `bb_width >= 0.065` on D0 and expire
  immediately on D1/D2 if price reclaims the D0 broken 20-session boundary.
- PUT is vetoed when `vix_chg_pct <= -0.05`, `ret_3d > 0`, and
  `range_position_10d >= 0.80` (bullish continuation / squeeze risk).
- PUT is also vetoed when `ret_3d >= 0`, `ret_5d >= 0`,
  `trend_efficiency_10d < 0.10`, and `range_position_10d` is between 0.45 and
  0.70 inclusive.

Execution-time CALL gate:

- Applies only when the CALL came from promotion.
- If D1 opens at least 0.20% below the signal-day close, entry waits until live
  spot reaches `signal_day_close_1515 * 1.001`.
- This decision is stored as `entry_action`; it does not modify signal-time
  `effective_prediction` or its precision/recall.
- Historical PnL uses the D1 daily high as a reclaim proxy because intraday spot
  timestamps are not available in the daily feature record.

Production summary metrics are calculated from `effective_prediction` for both
in-sample and sequential walk-forward evaluation. Walk-forward watches are rebuilt
chronologically inside the out-of-sample sequence to prevent look-ahead leakage.

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

`strategy_grid_definitions.csv` intentionally lists raw strategy variants only.
The watch/promotion layer is not listed as another variant because it combines
multiple production strategies across D0/D1/D2; see the production promotion
section above.

### VectorBT Research UI fields
| Field | Meaning |
|---|---|
| `Target pct(s)` | Option-premium profit target override used by the research replay. Current choices are 1%, 2%, 5%, 7%, and 10%. Multiple selections run a grid. |
| `Stop loss pct(s)` | Option-premium stop override. Current choices are 1%, 2%, 3%, and 5%, with 2% selected by default. Multiple selections run a grid. It is independent of `STRESS_SL_PCT` and `CALM_SL_PCT`. |
| `Variant filter` | Runs all strategies or only selected research variants. This does not change the production promoted roster. |
| `Strategy type` | Filters existing leaderboard rows by canonical strategy authority. |
| `Strategy family` | Filters existing leaderboard rows by canonical family; combines with Strategy type. |

The Research UI defaults the target grid to 5%.

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
- VectorBT trade replay keeps historical entry/exit facts intact, overlays the
  current target/stop policy for visibility, and scenario-sizes `quantity`,
  `gross_pnl`, and `net_pnl` from `PAPER_TRADING_CAPITAL` and
  `PAPER_CAPITAL_PER_TRADE_PCT`.
