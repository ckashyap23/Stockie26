# Backtests

Backtests consume production/research logic; they are not the source of truth for
strategy definitions. Strategy authority lives in
`src/technical_analysis/strategy_families.yaml`.

## Workflows

| Workflow | Path | Purpose |
|---|---|---|
| Production option selection | `backtest/production/pipeline_upsert_option_selections.py` | Builds historical `NiftyOptionSelection` rows from production predictions. |
| Production PnL replay | `backtest/production/pipeline_backtest_pnl.py` | Replays selected options against snapshots and actual paper fills where available. |
| Research strategy grid | `backtest/vectorbt_research/strategy_grid.py` | Scores raw strategy variants outside the production cascade. |
| Executed trade replay | `backtest/vectorbt_trades/cli.py` | Replays actual paper/live trades as a portfolio. |

## Outputs

Generated artifacts are written below `output/backtest/NIFTY/` and should be
treated as reports, not configuration.

## Production Contract

- Production backtesting uses `effective_prediction`.
- Option replay preserves actual paper fills/exits when present.
- Missing historical option data may limit older replay windows.
- Research-grid results do not automatically change production authority; update
  `strategy_families.yaml` manually after review.
