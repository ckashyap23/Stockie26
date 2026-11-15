# Backtests and replay

The strategy source of truth is `analysis/strategy_registry.yaml`. Research results do not automatically change production authority. Use `effective_prediction` as the actionable direction; `final_prediction` remains the raw cascade output before guards.

| Workflow | Command | Purpose |
| --- | --- | --- |
| Production prediction evaluation | `python -m technical_analysis_service run production.predict -- --help` | Run the cascade, grade resolved NIFTY outcomes and persist predictions |
| Research strategy grid | `python -m technical_analysis_service run research -- --help` | Grade the full registry's raw strategy variants |
| Executed paper-trade replay | `python -m technical_analysis_service run trades.replay -- --mode paper` | Portfolio replay of recorded paper fills |
| Maintenance option-selection replay | `python -m technical_analysis_service run production.option-backfill -- --help` | Explicit stored-selection repair; not a normal prediction-dashboard job |
| Maintenance option P&L replay | `python -m technical_analysis_service run production.backtest -- --help` | Explicit stored-option P&L analysis; separate from prediction labels |

The normal Research and Production prediction views evaluate NIFTY labels and returns; option availability does not define those metrics. Option maintenance tools are retained for deliberate use and are not invoked by the normal prediction views. The Trades view replays recorded executions; live broker-order execution is not implemented by this extraction.

Generated artifacts live under the configured output directory's `backtest/NIFTY/` subtree. They are reports, not strategy configuration. Database-dependent workflows require configured credentials and existing input tables. See [operations.md](../docs/operations.md) for timing and data prerequisites.
