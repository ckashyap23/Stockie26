# Technical Analysis

Production technical analysis predicts NIFTY direction and feeds option
selection. The durable prediction record is `NiftyPrediction`.

## Pipeline Shape

```text
SignalFeatureDaily  (EOD features: OHLC, ATR, regime inputs)
  + open-gap features (9:20 AM: nifty_gap_pct, gift_gap_pct, gap_open_atr ...)
  -> global-index context
  -> regime classification (calm / stress via VIX + vol)
  -> strategy signals (SIGNAL + VOTE_ONLY families)
  -> 6-step family-vote cascade
  -> weak-opposition check
  -> D0/D1/D2 watch promotion
  -> gap guard + event gate
  -> effective_prediction
  -> option selection
```

## Strategy Authority

`src/technical_analysis/strategy_families.yaml` is the source of truth.

| Type | Role |
|---|---|
| `SIGNAL` | Drives hard-trade cascade and seeds watches |
| `VOTE_ONLY` | Contributes family votes; cannot trade or seed watches |
| `RESEARCH` | Research grid only; no production participation |

The cascade requires ≥2 SIGNAL family CALLs (or PUTs) with weak opposition to
fire. If only VOTE_ONLY families accumulate votes, a watch seed is created
instead.

## Watch Promotion

Watch processing is chronological. A D0 watch created by a SIGNAL family
promotes on D1 or D2 when:
- A **different family** fires the same direction, **and**
- Opposition is weak (≤1 VOTE_ONLY family, no SIGNAL)

Same-family re-firing never promotes. Price-action confirmation is disabled
(`watch_only_price_action_promotion: enabled: false`): a watch that reaches D2
without an independent confirmer simply expires.
Strong opposition (any SIGNAL on the opposite side) kills the watch immediately.

## UI Contract

The dashboard, option selection, production PnL, and paper trading use
`effective_prediction`. The Production UI exposes `prediction_strategy` for the
actionable source and `watched_strategy` for active/promoted watch lineage.
