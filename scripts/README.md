# Scripts

Scripts are operational entry points around the production database. Prefer the
daily wrappers for normal use and the Common/backfill scripts for maintenance.

## Daily NIFTY

- `daily_market_refresh.py`: refreshes daily underlying market data and chains
  `daily_nifty_prediction.py` with a short lookback.
- `daily_open_gap.py`: 9:22 AM IST cron; fetches NIFTY and GIFT NIFTY 9:15 AM
  5-minute candles, saves OHLC to `UnderlyingCandle5m` / `GiftNiftySnapshot`,
  computes open-gap features, and upserts them to `SignalFeatureDaily` for
  signal_date = D-1.
- `daily_nifty_signal.py`: orchestrates prediction and option selection. The
  prediction stage writes `final_prediction`; the guard layer writes
  `effective_prediction`, which option selection consumes.
- `daily_nifty_prediction.py`: runs only the production prediction cascade.
- `daily_macro_refresh.py`: 9:00 AM IST job that stores prior-session Brent,
  US 10Y, USD/INR, provisional FII/DII cash values, and NSE participant-wise
  FII index-futures OI, then updates macro features on `SignalFeatureDaily`.
- `daily_option_selection.py`: runs only option selection for an existing signal.
- `daily_get_kite_access_token.py`: interactive Kite access-token refresh used
  before market jobs when the prior token has expired.
- `daily_paper_entry.py`, `daily_paper_monitor.py`, `daily_paper_report.py`:
  manage the paper execution lifecycle. Entry uses a marketable limit near
  9:20 AM for a slightly-ITM option closest to 21 DTE. Target and stop are a
  delta-adjusted OCO pair. Target touches advance a cascade anchored to the
  initial target/stop distances; stop touches close the position. Paper
  positions have no time exit.
- `daily_optionInstrument_refresh.py`, `daily_NIFTYoption_snapshot.py`, and
  `daily_NIFTYoption_OHLC.py` maintain the current option master and forward
  paper-trading market data. The OHLC job does not run historical option
  selection or PnL backtests.
- `daily_NIFTYGift_snapshot.py` captures open/close GIFT NIFTY references.
- `daily_signal_summary.py` and `daily_paper_report.py` produce operational
  summaries from the durable database records.

## Common Utilities

- `load_daily_index_data.py`: refreshes global-index OHLC/context. Supports
  `--mode us-eur`, `--mode asia-partial`, `--mode asia-final`, and historical
  `--mode asia-backfill`. Asian partial returns require an exact-date morning
  snapshot and a genuine final close from the preceding session.
- `calculate_underlying_features.py`: rebuilds underlying feature rows.
- `calculate_macro_features.py`: rebuilds 1-day/5-day macro inputs, component
  z-scores, and the renormalized macro risk-off composite.
- `export_options_trading_log.py`: exports selected production option contracts
  with entry/exit details, delta-adjusted levels, daily OHLC excursions,
  observed post-entry movement, realized labels, and PnL.
- `calculate_option_snapshot_calc.py`: computes option Greeks/IV fields.
- `analyze_precision_misses.py`: exports precision and recall miss CSVs with
  D-2/D-1/D/D+1/D+2 signal-feature context.
- `export_production_strategy_metrics_log.py`: exports every production signal
  date with point-in-time features, global-index context, binary strategy fire
  columns, and the realized trade label.
- Production prediction replay also writes `NIFTY_strategy_measurement.csv`
  with fire counts, directional precision, wrong-way rate, and outcome counts
  for every production signal strategy.
- `export_db_to_excel.py`: exports selected DB data for inspection.

## Backfills

`scripts/backfill_NIFTY/` is limited to the historical inputs needed to rebuild
research and production NIFTY prediction features:

- `backfill_underlying.py`: NIFTY daily and 5-minute underlying candles.
- `backfill_nifty_volume.py`: NIFTY volume proxy.
- `backfill_india_vix.py`: India VIX history.
- `backfill_nifty_option_pcr.py`: aggregate NIFTY put/call OI ratio.
- `backfill_nifty_oi_regime.py`: aggregate NIFTY open-interest regime.
- `backfill_gift_nifty.py`: historical GIFT NIFTY/open-gap inputs.
- `backfill_nse_participant_oi.py`: official NSE participant-wise OI.

For example:

```powershell
python scripts/backfill_NIFTY/backfill_nse_participant_oi.py --start 2024-01-01 --end 2026-10-02
```

The participant-OI backfill uses the union of NSE calendar and NIFTY signal
dates, fails if an expected archive is missing, upserts raw FII index-futures
long/short contracts, and recalculates `fii_idx_long_ratio` and
`fii_idx_long_ratio_chg5`. Macro feature calculation robust-normalizes the
five-session ratio change into `fii_idx_5d_z`; provisional FII/DII cash fields
remain stored for diagnostics but do not enter `macro_risk_off`.

There are no historical option-contract selection, option OHLC, option
snapshot, or option PnL replay backfills. Those datasets are forward-only for
paper trading.

`macro_risk_off` uses robust clipped crude and USD/INR z-scores with weights
0.25 and 0.25, plus the US 10Y z-score at weight 0.50. FII positioning remains
available for research but is excluded from the production composite.

## Notes

- Production jobs assume `.env` and Supabase connectivity.
- Render/cron commands should use repository-relative paths.
- Generated CSVs and logs belong under `output/`.
