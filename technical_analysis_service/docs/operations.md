# Operating the technical-analysis service

This is the current runbook for the extracted service. It consolidates the previous daily-run, local-run and scheduler notes. Commands use installed Python modules; the old root `scripts/` paths have been retired. All times below are Asia/Kolkata (IST). No live scheduled tasks were changed during this cleanup.

## Configuration and launch

Install from the repository root with `python -m pip install "./technical_analysis_service[broker-login]"`. The optional broker-login extra is needed only for automated Kite token generation; install Chromium with `python -m playwright install chromium` if using it.

The root dashboard launcher preserves the existing root `.env` and `output/` locations:

```powershell
.venv/Scripts/python.exe flask_app.py
```

It listens on `127.0.0.1:5000`. Standalone service commands normally load `technical_analysis_service/.env`. To run them against the same local settings and artifacts as the root launcher, explicitly select those paths in the process environment:

```powershell
$env:TECHNICAL_ANALYSIS_ENV_FILE = (Resolve-Path .env).Path
$env:TECHNICAL_ANALYSIS_OUTPUT_DIR = Join-Path (Get-Location).Path 'output'
.venv/Scripts/python.exe -m technical_analysis_service tasks
```

For scheduled tasks, make those variables available to the task account, or maintain a service-specific `.env`. Use absolute paths for local broker-token files. Credentials belong in the environment or the selected `.env`, never in job arguments.

## Trading-date contract

For execution session D, `NiftyPrediction.signal_date` is the preceding trading session and `next_trade_date` is D. Completed price, technical, macro, PCR, OI and VIX inputs come from the signal session. Morning global-index, GIFT NIFTY and open-gap context is joined before the execution-session prediction is finalized. Outcome and quality labels use future observations only for grading; they must never become strategy inputs.

## Daily sequence

Generated from `scheduled_jobs.JOBS`, the roster the Windows tasks and the Railway scheduler both run (each job has an allowed IST window, an NSE-holiday check and same-day prerequisites).

Use `python -m technical_analysis_service run <task> -- <arguments>` for the task names below. `run <task> -- --help` prints the underlying workflow's supported flags.

| Time IST | Job | Workflow / arguments | Needs same-day success of |
|---|---|---|---|
| 03:15 | us-index | `market.global --mode us-eur` | - |
| 07:30 | kite-token | `module scripts.daily_NIFTY.daily_get_kite_access_token` | - |
| 08:30 | option-instruments | `options.instruments --type INDEX --underlying NIFTY --underlying BANKNIFTY` | kite-token |
| 09:00 | macro-refresh | `market.macro` | - |
| 09:05 | asia-partial | `market.global --mode asia-partial` | - |
| 09:11 | gift-preopen | `market.gift --mode pre-open` | kite-token |
| 09:15 | option-snapshot | `options.snapshot --underlying NIFTY --snapshot-label OPEN_0915` | option-instruments |
| 09:20 | gift-open | `market.gift --mode open` | kite-token |
| 09:22 | open-gap | `market.open-gap` | gift-open |
| 09:24 | prediction | `production.signal --underlying NIFTY --model-version cascade_v1` | us-index, option-snapshot, open-gap, macro-refresh, asia-partial |
| 09:27 | paper-entry | `paper.entry --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300` | prediction |
| 09:30-15:25 every 5 min, 15:29 | paper-monitor | `paper.monitor --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300 --skip-pending-entry` | - |
| 15:15 | gift-close | `market.gift --mode close` | kite-token |
| 15:32 | paper-report | `paper.report --underlying NIFTY --model-version cascade_v1` | - |
| 15:45 | asia-final | `market.global --mode asia-final` | - |
| 15:47 | market-refresh | `market.refresh --underlying NIFTY` | kite-token |
| 16:00 | option-ohlc | `options.ohlc --underlying NIFTY` | kite-token |
| 16:10 | signal-summary | `module scripts.daily_NIFTY.daily_signal_summary --underlying NIFTY --model-version cascade_v1` | - |

Additional module commands:

```powershell
python -m technical_analysis_service.scripts.daily_NIFTY.daily_get_kite_access_token --help
python -m technical_analysis_service.diagnostics.daily_data_quality_check --help
python -m technical_analysis_service.scripts.daily_NIFTY.daily_signal_summary --help
```

The quality check returns exit code 2 for missing/stale required inputs. Stop dependent jobs when an upstream prerequisite fails. The 09:20–09:27 jobs form a dependency chain. `production.signal` already invokes prediction and selection; scheduling separate copies would duplicate work. Capture the 09:15 option snapshot before the selector's 09:20 as-of cutoff.

Weekday triggers alone do not exclude NSE holidays. Check `TradingCalendar` or disable holiday runs. Several existing scripts guard weekends only; do not assume every module checks holidays. Prevent overlapping monitors and coordinate externally scheduled writers with API/dashboard jobs.

## Current paper-execution policy

Entry uses live quotes to simulate a marketable-limit fill and records the underlying level and option delta. The target and stop form a delta-adjusted OCO pair. A target touch advances the anchored cascade and recreates the OCO; a stop touch closes the position.

The current monitor defaults to a same-day forced exit at 15:15 IST (`--force-exit-time`), with stored per-trade overrides respected. `--no-eod-force-exit` explicitly disables this behavior. Earlier notes claiming that paper trading has no time exit were stale. This cleanup updates the documentation and does not change execution code or the chosen runtime flags.

The monitor also retries pending entries by default. Use `--skip-pending-entry` only when disabling that retry is intentional. Reports read durable trade results, realized exits and the most recent monitored executable bid; they do not manufacture an official exchange closing mark for open positions.

StreakContinuationCall is a production SIGNAL with its own paper policy: half premium allocation rounded down to whole lots, underlying-move target 0.6% and stop 0.4% converted to option levels through delta adjustment, fixed target exit (no cascade ratchet), and forced exit at 15:10 IST even if general EOD exits are disabled. Include the 15:10 monitor invocation; the actual fill is the first executable quote observed at/after cutoff. Entry follows the existing 09:24 signal / 09:27 paper chain, not a retroactive opening fill. Missing opening-gap inputs cause abstention. Existing production guards remain active.

On 11 November 2026 the user removed the production-wide CALL GAP_UP veto. The PUT GAP_DOWN veto, event gate and individual strategies' own gap limits remain active. No additional half-size-on-gap rule was enabled; each strategy retains its normal allocation.

## Token callback port

The Kite token tool defaults to callback port 5000, which conflicts with the root dashboard. The standalone backend defaults to 5001. If both run locally, use a third registered callback port, for example 5002, and set `KITE_REDIRECT_PORT` to match the exact redirect URL registered with the broker. Otherwise stop the conflicting server during token refresh. Changing documentation does not change the registered broker application or local credentials.

## Maintenance

Diagnostic exports and miss analysis live in `diagnostics/`. Input repairs live in `scripts/backfill_NIFTY/`. Read [the script catalog](../scripts/README.md) and [backtest contracts](../backtest/README.md) before replaying or rebuilding stored data. Historical option-selection and P&L tools remain explicit maintenance workflows; normal Research and Production prediction evaluation uses NIFTY labels and returns.
