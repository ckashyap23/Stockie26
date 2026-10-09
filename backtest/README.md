# Backtests

Backtests consume production/research logic; they are not the source of truth for
strategy definitions. Strategy authority lives in
`src/technical_analysis/strategy_registry.yaml`.

## Workflows

| Workflow | Path | Purpose |
|---|---|---|
| Production prediction backtest | `src/technical_analysis/cascade/pipeline.py` | Scores production NIFTY predictions against realized NIFTY movements. |
| Research strategy grid | `backtest/vectorbt_research/strategy_grid.py` | Scores raw strategy-variant NIFTY predictions outside the production cascade. |
| Executed trade replay | `backtest/vectorbt_trades/cli.py` | Replays actual paper/live trades as a portfolio. |

The Research and Production Backtest dashboards are prediction-only. Historical
option selection and PnL replay scripts are retained as legacy maintenance
utilities, but are not invoked by either backtest pipeline or dashboard.

## Outputs

Generated artifacts are written below `output/backtest/NIFTY/` and should be
treated as reports, not configuration.

## Production Contract

- Production backtesting uses `effective_prediction`.
- `final_prediction` stays as the raw cascade output; guards are represented in
  `effective_prediction`.
- Production and research metrics use NIFTY prediction labels and realized
  NIFTY movement only; option availability does not affect those metrics.
- Research-grid results do not automatically change production authority; update
  `strategy_registry.yaml` manually after review.
