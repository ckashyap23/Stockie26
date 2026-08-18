# Database Schema

Supabase/Postgres is the durable store for production data. Local CSVs under
`output/` are reports and developer artifacts.

## Key Tables

| Table | Purpose |
|---|---|
| `SignalFeatureDaily` | Daily NIFTY feature rows used by prediction. |
| `GlobalIndexOhlc` | Global-index OHLC and return context. |
| `NiftyPrediction` | Production `final_prediction`, guarded `effective_prediction`, strategy metadata, guard reasons, and labels. |
| `NiftyOptionSelection` | Selected option contract and planned entry/exit levels. |
| `PaperExecutionSignal` | Planned and entered paper signals. |
| `PaperTradeResult` | Paper/live trade lifecycle result. |

## Schema Contract

- Migrations live under `src/data_manager/db/migrations/`.
- Upsert helpers should preserve existing durable rows and add new nullable
  fields through migrations or guarded DDL.
- Prediction consumers should use `effective_prediction` for actionable
  direction.
- Retired prediction columns for regime, drift override, watch promotion, and
  promoted-call entry gating have been dropped from live tables.
