# Technical Analysis

Current production flow:

```text
SignalFeatureDaily
  -> common production strategy signals
  -> cascade final_prediction
  -> guard layer effective_prediction
  -> option selection / paper trading / backtesting
```

Production strategy behavior:

| Type | Behavior |
|---|---|
| `SIGNAL` | Can directly create CALL/PUT candidates |
| `RESEARCH` | Research grid only; excluded from production predictions and paper trading |

Any production `SIGNAL` strategy can trigger a prediction. If CALL and PUT
strategies both fire on the same date, the cascade resolves the conflict using
historical precision. Event-day and gap guards live after the cascade and only
change `effective_prediction`.

Retired concepts: regime-specific strategy routing, vote-only production
strategies, watch seeding/promotion, and drift override.
