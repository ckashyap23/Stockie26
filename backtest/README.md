# Backtest package

This directory contains the implementations for three separate backtesting
workflows. For commands, configuration, UI fields, promotion rules, and operating
instructions, use the team runbook:

- [RunBacktesting.md](../TeamDocs/RunBacktesting.md)

## Package index

### Production pipeline

Location: [`backtest/production/`](production/)

- `pipeline_upsert_option_selections.py` builds historical option selections from
  the canonical `effective_prediction` stored in `NiftyPrediction`.
- `pipeline_backtest_pnl.py` replays production selections against intraday
  `OptionSnapshot` prices, preserving actual paper-trade fills/exits where
  recorded and using snapshot approximation only when no execution exists.
- `pipeline_backtest_optionselection.py` is a legacy read-only research/E2E tool;
  it does not populate the production tables.

Production prediction and promotion logic lives in
`src/technical_analysis/cascade/`. Option construction lives in
`src/technical_analysis/optionselection/`.

### Research strategy grid

Location: [`backtest/vectorbt_research/`](vectorbt_research/)

This workflow evaluates raw strategy variants without the production cascade's
precision-floor selection. See the
[VectorBT research README](vectorbt_research/README.md) for its design, extension
points, and artifact contracts.

The production watch/promotion layer is not a research-grid variant. It combines
strategy signals across D0/D1/D2 and is documented in the
[production promotion section](../TeamDocs/RunBacktesting.md#production-watch-and-promotion-layer).

### Executed paper/live trades

Location: [`backtest/vectorbt_trades/`](vectorbt_trades/)

This workflow evaluates actual recorded fills from `PaperExecutionSignal` and
`PaperTradeResult`. It does not simulate entries from option snapshots. Replay
quantity and total PnL are scenario-sized from the actual entry fill using
`PAPER_TRADING_CAPITAL` and `PAPER_CAPITAL_PER_TRADE_PCT`; persisted execution
rows remain unchanged.

## Ownership boundary

Backtest modules consume production logic; they are not its source of truth.

- Signal, regime, cascade, and promotion rules:
  `src/technical_analysis/cascade/`
- Option-selection rules: `src/technical_analysis/optionselection/`
- Paper execution and exit rules: `src/execution/`
- Durable prediction, selection, and execution records: Supabase

Generated files under `output/backtest/` are artifacts, not configuration or
strategy definitions.

The Flask Production summary can run the precision/recall miss analyzer and
download both generated CSVs. The Research leaderboard exposes combined
strategy-type and strategy-family filters; these filter displayed results and do
not change the strategy registry.
