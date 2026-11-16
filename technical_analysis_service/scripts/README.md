# Operational scripts

Use `python -m technical_analysis_service run <task> -- --help` for normal workflows. The full sequence, configuration paths, IST timings and current paper-exit policy are in [the operating runbook](../docs/operations.md).

| Area | Active modules / task names |
| --- | --- |
| Market and global inputs | `market.refresh`, `market.global`, `market.macro`, `market.gift`, `market.open-gap` |
| Options data | `options.instruments`, `options.snapshot`, `options.ohlc` |
| Feature calculation | `features.underlying`, `features.options`, `features.macro` |
| Predictions and selection | `production.predict`, `production.signal`, `production.options` |
| Paper lifecycle | `paper.entry`, `paper.monitor`, `paper.report` |
| Token generation | `scripts.daily_NIFTY.daily_get_kite_access_token` |
| Operational signal summary | `scripts.daily_NIFTY.daily_signal_summary` (prints the day's prediction and option) |
| Calendar maintenance | `scripts.Common.build_trading_calendar`; optional `calendar` dependency extra |
| Morning email | `scripts.daily_NIFTY.daily_morning_email` (job `morning-email`, 09:32 IST): prediction, news score, option and paper-trade details, news articles CSV attached; Resend |
| Drift feature export | `scripts.Common.build_nifty_drift_csv` |

Direct modules have the prefix `technical_analysis_service.`. Run them with `python -m <module> --help` to inspect their flags. The token tool needs configured broker credentials even when imported; use the broker-login setup in the runbook.

`daily_market_refresh` also chains a short prediction/label refresh. `production.signal` already runs prediction followed by option selection. Do not schedule duplicate copies of those stages. Paper monitoring defaults to a same-day 15:15 forced exit unless explicitly disabled; target touches advance the anchored OCO cascade, and stop touches close positions.

## Input repairs

Historical input repairs live in `scripts/backfill_NIFTY/`:

- `backfill_underlying`: underlying daily and five-minute candles.
- `backfill_nifty_volume`: futures-volume proxy.
- `backfill_india_vix`: India VIX history.
- `backfill_nifty_option_pcr`: aggregate option put/call ratios.
- `backfill_nifty_oi_regime`: aggregate open-interest context.
- `backfill_gift_nifty`: GIFT/open-gap inputs.
- `backfill_nse_participant_oi`: official participant-wise FII futures positions.
- `backfill_market_macro`: historical macro inputs, with optional official cash-flow CSV input.

For example:

```powershell
python -m technical_analysis_service.scripts.backfill_NIFTY.backfill_nse_participant_oi --help
```

Macro inputs and feature normalization are described in [NIFTYFeatures.md](../docs/NIFTYFeatures.md). These repairs can write the configured database and must be invoked deliberately. Forward option snapshots and audit OHLC remain separate from prediction-label evaluation.

## Diagnostics

Miss analysis, data-quality inspection and exports are in [`diagnostics/`](../diagnostics/README.md). They are no longer listed as active Common scripts. Generated artifacts belong under the configured output directory. No retired root `scripts/` path is required to run this service.
