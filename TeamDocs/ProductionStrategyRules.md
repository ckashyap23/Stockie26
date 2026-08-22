# Production Strategy Rules

Current decision chain:

```text
production strategy fires
  -> cascade conflict tie-break
  -> final_prediction
  -> guard layer
  -> effective_prediction
```

## Production Strategies

| Strategy | Type | Direction |
|---|---|---|
| `PullbackCall_TrendIntact` | `SIGNAL` | CALL |
| `PullbackCall_DeepWashout` | `SIGNAL` | CALL |
| `RsiReversion_6040` | `SIGNAL` | TWO_SIDED |
| `DRIFT_PROBE` | `SIGNAL` | TWO_SIDED |
| `DeclineContinuationPut_ATR` | `SIGNAL` | PUT |
| `BreakdownPut_20d` | `SIGNAL` | PUT |

Any single `SIGNAL` strategy can produce a CALL or PUT candidate. If both sides
fire on the same signal date, the cascade chooses the side with stronger
historical precision.

## Research Only

`DeclineContinuationPut_ATR_v2` and `ExpansionVotes_Strong` remain available in
the research grid but are excluded from production backtesting, daily
predictions, option selection, and paper trading.

## Guard Layer

Guards run after the cascade:

| Guard | Effect |
|---|---|
| Event-day guard | Can suppress a CALL/PUT to `NO_POSITION` on scheduled event-risk dates |
| Gap guard | Can suppress same-direction overextended opening gaps |

`final_prediction` remains the raw cascade output. `effective_prediction` is the
guarded output used by option selection and paper trading.

Retired concepts: regime branching, vote-only production strategies, watch
seeding/promotion, and drift override.
