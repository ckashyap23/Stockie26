# Run Locally

This repo is DB-first for the NIFTY production flow. Local files under `output/`
are developer artifacts only and are ignored by git.

## Prerequisites

- Python virtual environment activated.
- `.env` present at repo root.
- Supabase reachable through `SUPABASE_CONN_STR`.
- Kite access token refreshed for the trading day if running market or option
  snapshot/OHLC jobs.

Required production environment variables:

```text
DATABASE_PROVIDER=supabase
SUPABASE_CONN_STR=<postgres connection string>
KITE_API_KEY=<kite api key>
KITE_API_SECRET=<kite api secret>
```

Percentages are decimals (`0.01` means 1%). See `.env.example` for separate
NIFTY targets, option targets/stops, and both horizon settings. Restart Flask
after changing `.env`; newly launched daily jobs read the latest values.

## Daily NIFTY Signal Flow

Run upstream refresh jobs first:

```powershell
python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY
python scripts/Common/load_daily_index_data.py --no-local-output
python scripts/daily_NIFTY/daily_optionInstrument_refresh.py --underlying NIFTY
python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py
python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY
```

If the option snapshot job did not calculate IV/Greeks in the deployed flow,
run the calc job for the same date before signal generation:

```powershell
python scripts/Common/calculate_option_snapshot_calc.py --from-date 2026-06-26 --to-date 2026-06-26
```

Then run the cron-friendly signal wrapper:

```powershell
python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1
```

The wrapper runs production prediction, persists `NiftyPrediction`, runs option
selection for the latest unresolved prediction, persists `NiftyOptionSelection`,
and prints `FINAL_SIGNAL_JSON=...` with one actionable option token/symbol plus
target levels.

To rerun option selection for an existing prediction row without rerunning the
prediction step:

```powershell
python scripts/daily_NIFTY/daily_nifty_signal.py --skip-prediction --trade-date 2026-06-25 --model-version cascade_v1
```

Stop loss is disabled by default. Enable it explicitly if needed:

```powershell
python scripts/daily_NIFTY/daily_nifty_signal.py --model-version cascade_v1 --stop-loss-pct 0.01
```

## Individual Jobs

Prediction only:

```powershell
python scripts/daily_NIFTY/daily_nifty_prediction.py --model-version cascade_v1
```

Option selection only:

```powershell
python scripts/daily_NIFTY/daily_option_selection.py --trade-date 2026-06-25 --model-version cascade_v1
```

Option daily OHLC only:

```powershell
python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY
```

Historical option daily OHLC backfill:

```powershell
python scripts/backfill_NIFTY/backfill_NIFTYoptions_OHLC.py --from-date 2026-04-01 --to-date 2026-06-26 --underlying NIFTY
```

`OptionOhlc` is intentionally separate from `OptionSnapshot` and
`OptionSnapshotCalc`. Historical option OHLC depends on what Kite returns for
expired option tokens; missing older contracts can be a Kite availability limit,
not necessarily a database issue.

Render cron commands must use repository-relative paths exactly:

```bash
python scripts/Common/load_daily_index_data.py --no-local-output
python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY
```

News sentiment is intentionally not wired into production NIFTY prediction yet.
Run it only for research or if a separate cron needs to maintain sentiment data:

```powershell
python scripts/daily_NIFTY/daily_news_sentiment.py --sector-classifier keyword
```

## Flask App

The Flask dashboard reads Supabase directly. The Daily Prediction table defaults
to 2026-01-01 through the current date and has prediction filters. The summary's
**Analyze Misses** action runs `scripts/Common/analyze_precision_misses.py` and
downloads both stress miss reports. Trades reads executed paper trades from the
database and displays timestamps in IST.

```powershell
python flask_app.py
```

Open `http://127.0.0.1:5000`.

## Validation

```powershell
python -m pytest tests/test_cascade_option_signal_mapper.py tests/test_underlying_prediction.py
python -m pytest tests/test_signal_strength.py tests/test_paper_execution.py tests/test_vectorbt_trades.py
python -m pytest tests/test_news_sentiment.py tests/test_global_index_features.py
```

## Troubleshooting

Port 5000 already in use:

```powershell
netstat -ano | findstr ":5000"
Stop-Process -Id <PID> -Force
```

Missing option selection usually means one of these is absent for the signal
date: `NiftyPrediction`, `OptionInstrument`, `OptionSnapshot`, or
`OptionSnapshotCalc`. Missing daily OHLC rows live in `OptionOhlc` and do not by
themselves block production option selection.
