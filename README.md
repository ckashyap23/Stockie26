# Stockie26

Stockie26 is a DB-first NIFTY options signal system. It combines daily market
refresh jobs, a common-date technical-analysis cascade, option selection, paper
execution, and dashboard review.

## Core Flows

- **Research**: runs the VectorBT strategy grid to compare raw strategy variants.
- **Production Strategies**: reviews production predictions, guarded effective
  predictions, option selections, PnL replay, and miss-analysis downloads.
- **Trades**: reviews actual paper/live execution results and VectorBT replay.

## Current Production Model

- Production starts from `SignalFeatureDaily` rows and global-index context.
- `strategy_families.yaml` is the source of truth for strategy authority.
- `effective_prediction` is the canonical CALL/PUT/NO_POSITION value used by
  option selection, paper trading, backtesting, and the dashboard.
- Production `SIGNAL` strategies all generate direct CALL/PUT candidates. If
  both sides fire on the same date, the cascade tie-breaks by historical
  precision. `RESEARCH` strategies appear in the research grid only.
- `final_prediction` is the raw cascade output. A separate guard layer can
  suppress it to `NO_POSITION`, producing `effective_prediction`.
- Global index returns are persisted for audit; strategy-level global suppressors
  are disabled.

## End-To-End Logic

```text
market refresh
  -> SignalFeatureDaily
  -> production SIGNAL strategies
  -> cascade final_prediction
  -> event/gap guard layer effective_prediction
  -> NiftyOptionSelection
  -> PaperExecutionSignal
  -> PaperTradeResult / replay / dashboard
```

## Where To Look

| Area | Path |
|---|---|
| Flask dashboard | `flask_app.py` |
| Daily scripts | `scripts/daily_NIFTY/` |
| Utility/backfill scripts | `scripts/Common/`, `scripts/backfill_NIFTY/` |
| Prediction cascade | `src/technical_analysis/cascade/` |
| Strategy registry | `src/technical_analysis/strategy_families.yaml` |
| Option selection | `src/technical_analysis/optionselection/` |
| Production backtests | `backtest/production/` |
| Research grid | `backtest/vectorbt_research/` |
| Paper trade replay | `backtest/vectorbt_trades/` |
| Team notes | `TeamDocs/` |

## Local Use

Run the dashboard:

```powershell
python flask_app.py
```

Open `http://127.0.0.1:5000`.

Operational commands and backtest entry points are summarized in
`run_local.md`, `scripts/README.md`, and `backtest/README.md`.
