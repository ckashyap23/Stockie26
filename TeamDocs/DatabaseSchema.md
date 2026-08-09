# Database Schema

Supabase/Postgres is the durable store for production data. Local CSVs under
`output/` are reports and developer artifacts.

## Key Tables

| Table | Purpose |
|---|---|
| `SignalFeatureDaily` | Daily NIFTY feature rows used by prediction. |
| `GlobalIndexOhlc` | Global-index OHLC and return context. |
| `NiftyPrediction` | Production prediction, effective signal, audit lineage, and labels. |
| `NiftyOptionSelection` | Selected option contract and planned entry/exit levels. |
| `PaperExecutionSignal` | Planned and entered paper signals. |
| `PaperTradeResult` | Paper/live trade lifecycle result. |

## Schema Contract

- Migrations live under `src/data_manager/db/migrations/`.
- Upsert helpers should preserve existing durable rows and add new nullable audit
  fields through migrations or guarded DDL.
- Prediction consumers should use `effective_prediction` for actionable
  direction.
