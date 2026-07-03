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

## Tests

```powershell
python -m pytest tests/test_underlying_prediction.py tests/test_vectorbt_strategy_grid.py
python -m pytest tests/test_signal_strength.py tests/test_paper_execution.py tests/test_vectorbt_trades.py
```
