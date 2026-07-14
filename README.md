# Stockie26

Stockie26 is a DB-first NIFTY options signal system. It combines daily market
refresh jobs, a regime-aware technical-analysis cascade, option selection,
paper execution, and dashboard review.

## Core Flows

- **Research**: runs the VectorBT strategy grid to compare raw strategy variants.
- **Production Strategies**: reviews production predictions, promoted watch
  signals, option selections, PnL replay, and miss-analysis downloads.
- **Trades**: reviews actual paper/live execution results and VectorBT replay.

## Current Production Model

- Production starts from `SignalFeatureDaily` rows and global-index context.
- `strategy_families.yaml` is the source of truth for `TRADE_ELIGIBLE`,
  `WATCH_ONLY`, and `RESEARCH` authority.
- `effective_prediction` is the canonical CALL/PUT/NO_POSITION value used by
  option selection, paper trading, backtesting, and the dashboard.
- Watch-only signals can become actionable after D1/D2 same-family or
  price-action confirmation. CALL/PUT promotion has no hard market-profile veto.
- Global index returns are persisted for audit; strategy-level global suppressors
  are disabled for now.

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
