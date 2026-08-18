# NIFTY Prediction

The production prediction model is intentionally simple:

```text
strategy fires
  -> conflict tie-break if both directions fire
  -> final_prediction
  -> event/gap guard layer
  -> effective_prediction
```

`final_prediction` is the raw cascade answer. `effective_prediction` is the
tradeable answer after guard logic.

Production strategies:

| Direction | Strategy |
|---|---|
| CALL | `PullbackCall_TrendIntact` |
| CALL | `PullbackCall_DeepWashout` |
| TWO_SIDED | `RsiReversion_6040` |
| TWO_SIDED | `DRIFT_PROBE` |
| PUT | `DeclineContinuationPut_ATR` |
| PUT | `BreakdownPut_20d` |

Research-only strategies such as `DeclineContinuationPut_ATR_v2` and
`ExpansionVotes_Strong` can be evaluated in the research grid but are not used
for production backtesting, daily predictions, option selection, or paper
trading.

Retired concepts: regime branching, vote-only strategies, watch
seeding/promotion, and drift override.
