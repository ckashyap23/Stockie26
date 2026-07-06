# Technical Analysis

Current production scope: NIFTY direction prediction plus option selection.

## Daily Production Flow

```text
UnderlyingSnapshot
  -> SignalFeatureDaily
  -> NiftyPrediction (signal_date D0, next_trade_date D1)
  -> NiftyOptionSelection (execution trade_date D1)
```

Run the wrapper:

```powershell
python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1
```

Run pieces separately:

```powershell
python scripts/daily_NIFTY/daily_nifty_prediction.py --model-version cascade_v1
python scripts/daily_NIFTY/daily_option_selection.py --trade-date 2026-06-25 --model-version cascade_v1
```

## Backfill Inputs

Underlying OHLC and features:

```powershell
python scripts/backfill_NIFTY/backfill_underlying.py --underlying NIFTY --start 2026-01-01 --end 2026-06-30
python scripts/Common/calculate_underlying_features.py --underlying NIFTY --start 2026-01-01 --end 2026-06-30
```

Option snapshots and Greeks:

```powershell
python scripts/backfill_NIFTY/backfill_NIFTYoptions_from_historical.py --underlying NIFTY --start 2026-01-01 --end 2026-06-30
python scripts/Common/calculate_option_snapshot_calc.py --from-date 2026-01-01 --to-date 2026-06-30
```

## Main Code

| Area | Files |
|---|---|
| Prediction cascade | `src/technical_analysis/cascade/` |
| Option selection | `src/technical_analysis/optionselection/` |
| Research grid | `backtest/vectorbt_research/` |
| Production P&L | `backtest/production/` |

## Current Prediction Rules

For each NIFTY `signal_date`, production prediction follows this sequence:

```text
SignalFeatureDaily
  -> join India VIX and global-index features
  -> classify calm/stress regime
  -> evaluate the regime strategy roster
  -> apply historical precision eligibility
  -> select the direct cascade prediction
  -> process D0/D1/D2 watch state
  -> effective_prediction
  -> attach primary-strategy metadata
  -> upsert NiftyPrediction
```

### Inputs and regime

- `build_base()` reads NIFTY rows from `SignalFeatureDaily`; production does not
  use the research CSV as its input.
- The signal row contains information available on D0. Future prices and labels
  are used to grade resolved historical rows, not as strategy inputs.
- A row is `calm` when India VIX is below `13` and realised 10-day volatility is
  below `0.007`. All other rows are `stress`.
- Every calm-regime strategy additionally requires `bb_width >= 0.04`. Stress
  breakout/expansion guards continue to use `bb_width >= 0.065` where specified.
- Strategy precision must be above `0.55` in calm conditions or `0.70` in stress
  conditions, with at least five historical fires on that side.

### Strategy policy

`src/technical_analysis/strategy_families.yaml` is the metadata source of truth.

| `strategy_type` | Direct CALL/PUT | Create D0 watch | Confirm D1/D2 watch |
|---|---:|---:|---:|
| `TRADE_ELIGIBLE` | Yes | Yes | Yes |
| `WATCH_ONLY` | No | Yes | No |
| `RESEARCH` | No | No | No |

Guard filters are executed inside the strategy logic. A parent's YAML `guards`
list associates emitted guarded strategy names with the parent's family and
policy; it does not execute the filter itself.

### Direct cascade decision

- Only strategies firing on the signal date, passing precision eligibility, and
  marked `TRADE_ELIGIBLE` can make a direct prediction.
- Related variants are collapsed by family so one family does not receive
  duplicate votes.
- The highest-precision CALL family competes with the highest-precision PUT
  family. The higher precision wins; a tie or no eligible fire produces
  `NO_POSITION` in `final_prediction`.

### Watch and promotion decision

- Watch processing is sequential in trading-session order.
- When D0 `final_prediction` is `NO_POSITION`, a one-sided
  `TRADE_ELIGIBLE` or `WATCH_ONLY` fire may create a watch.
- Opposing CALL and PUT fires on D0 prevent watch creation.
- A watch can promote on D1 or D2 when a `TRADE_ELIGIBLE` strategy fires in the
  same direction and family, or when an active `WATCH_ONLY` family receives its
  own favorable price-action confirmation (CALL close rises; PUT close falls).
- A different-family confirmation or any `RESEARCH` source cannot promote a watch.
- Range families use their stored D0 boundary instead of generic close-to-close
  confirmation. They expire only after a clear 0.1% reclaim/loss of that boundary;
  a still-valid D2 setup remains eligible for confirmation.
- PUT squeeze/choppy-market vetoes and the oversold CALL participation veto may
  keep an otherwise valid confirmation from promoting.

The actionable result is:

```python
effective_prediction = (
    final_prediction
    if final_prediction != "NO_POSITION"
    else promoted_prediction
)
```

The persisted audit lineage includes the family, variant, and canonical strategy
type for the primary strategy, D0 watch, prior watch, and confirming strategy.
For a promoted signal, the primary strategy is normally the current
`TRADE_ELIGIBLE` confirming strategy.

The Production UI exposes `prediction_strategy` for the actionable source and
`watched_strategy` for a D0 setup created on that row. When the row remains
`NO_POSITION`, the watched strategy is awaiting D1/D2 confirmation and is not a
trade. Research leaderboard rows show
`strategy_type`; WATCH_ONLY rows also report separately attributed promotion
count, precision, and recall. Detailed D0-to-D1/D2 attribution is written to
`strategy_grid_watch_promotions.csv`.

Historical predictions rebuilt by the normal production run use eligibility fit
on all resolved production history and are therefore in-sample. The generated
summary also reports a rolling 120-day walk-forward result for out-of-sample
evaluation. Production evaluation starts on `2024-01-01`, with 2023 retained as
feature/indicator warm-up history. The durable production record is the `NiftyPrediction` table; the
current pipeline returns the assembled frame but does not refresh
`output/backtest/NIFTY/production/NIFTY_prediction.csv`.

## Current Option Rules

- `CALL` with strength `>= 65` can select `LONG_CALL`.
- `PUT` with strength `>= 65` can select `LONG_PUT`.
- Calls: ITM CE, delta `0.70` to `0.90`, 20 to 60 DTE.
- Puts: ITM PE, delta `-0.90` to `-0.70`, 20 to 60 DTE.
- Filters check spread, liquidity, theta burn, IV quality, and positive price.
- Option targets/stops use regime-specific `.env` settings and the actual paper
  fill. NIFTY labels use separate `*_NIFTY_TARGET_PCT` settings.
- `TRADE_HORIZON_DAYS` controls option holding; `UNDERLYING_LOOKBACK_DAYS`
  controls the NIFTY label and signal-quality window.

## Local Dashboard

```powershell
python flask_app.py
```

Open:

```text
http://127.0.0.1:5000
```

Tabs:

- Research: VectorBT strategy grid.
- Production: prediction, option selection, production P&L.
- Trades: paper trade results and VectorBT replay.

The Production date window defaults to 2026-01-01 through the request date.
**Analyze Misses** beside the accuracy/recall summary regenerates and downloads
`NIFTY_stress_in_sample_precision_misses.csv` and
`NIFTY_stress_in_sample_recall_misses.csv`.

## Tests

```powershell
python -m pytest tests/test_underlying_prediction.py tests/test_vectorbt_strategy_grid.py
python -m pytest tests/test_signal_strength.py tests/test_paper_execution.py tests/test_vectorbt_trades.py
```
