# Technical Analysis

Production technical analysis predicts NIFTY direction and feeds option
selection. The durable prediction record is `NiftyPrediction`.

## Pipeline Shape

```text
SignalFeatureDaily
  -> global-index context
  -> regime classification
  -> registry-authorized strategy signals
  -> direct cascade prediction
  -> D0/D1/D2 watch promotion
  -> effective_prediction
  -> option selection
```

## Strategy Authority

`src/technical_analysis/strategy_families.yaml` is the source of truth.

- `TRADE_ELIGIBLE`: can directly create CALL/PUT production signals.
- `WATCH_ONLY`: can create watch candidates and become actionable after
  confirmation.
- `RESEARCH`: appears in research only.

Precision and fire counts are audit/ranking values. They do not automatically
promote or demote strategies.

## Watch Promotion

Watch processing is chronological. A flat D0 row can create a CALL/PUT watch
when production-authorized strategies fire on one side only. A watch may promote
on D1 or D2 through same-family confirmation or allowed watch-only price action.

CALL/PUT promotion has no hard market-profile veto. Range-breakout watches still
respect their stored D0 boundary.

## UI Contract

The dashboard, option selection, production PnL, and paper trading use
`effective_prediction`. The Production UI exposes `prediction_strategy` for the
actionable source and `watched_strategy` for active/promoted watch lineage.
