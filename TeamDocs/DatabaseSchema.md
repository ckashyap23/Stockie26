# Database Schema

Supabase/Postgres is the durable store for production data. Local CSVs under
`output/` are reports and developer artifacts.

## Key Tables

| Table | Purpose |
|---|---|
| `SignalFeatureDaily` | Daily NIFTY feature rows used by prediction. |
| `UnderlyingSnapshot` | One daily OHLC row per underlying and trade date. |
| `UnderlyingCandle5m` | One five-minute candle per underlying and candle timestamp. |
| `GlobalIndexOhlc` | Global-index OHLC and return context. |
| `NiftyPrediction` | Production `final_prediction`, guarded `effective_prediction`, strategy metadata, guard reasons, and labels. |
| `NiftyOptionSelection` | Selected option contract and planned entry/exit levels. |
| `OptionInstrument` | Current option-contract master refreshed from Kite. |
| `OptionSnapshot` | Forward-only live option quotes used by paper selection/execution. |
| `OptionSnapshotCalc` | IV and Greeks calculated for live option snapshots. |
| `OptionOhlc` | Forward-only option audit OHLC, unique by instrument/date/interval. |
| `PaperExecutionSignal` | Planned and entered paper signals. |
| `PaperOrder` | Simulated entry and OCO order lifecycle. |
| `PaperTradeEvent` | Append-only paper execution event audit. |
| `PaperTradeResult` | Paper/live trade lifecycle result. |

## Schema Contract

- Migrations live under `src/data_manager/db/migrations/`.
- Upsert helpers should preserve existing durable rows and add new nullable
  fields through migrations or guarded DDL.
- Prediction consumers should use `effective_prediction` for actionable
  direction.
- Research and production backtests grade NIFTY predictions only. Historical
  option selections, snapshots, OHLC, and option PnL are not backtest inputs.
- Option market data is forward-only for paper trading. `OptionOhlc` permits
  one logical row for each option instrument, trade date, and interval,
  regardless of source.
- Production option selection and paper execution accept only predictions whose
  `primary_strategy_type` is `SIGNAL`.
- Retired prediction columns for regime, drift override, watch promotion, and
  promoted-call entry gating have been dropped from live tables.
