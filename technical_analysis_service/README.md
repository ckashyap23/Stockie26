# technical_analysis_service

Independent Stockie backend for the existing research strategies, production predictions, option selection and paper trading. The strategy calculations and trading rules are retained. This folder can be copied to another repository and installed on its own.

## Ownership

| Location | Responsibility |
| --- | --- |
| `analysis/` | Strategy registry, cascade engine, prediction features, scoring and option selection |
| `backtest/vectorbt_research/` | Full research strategy grid |
| `backtest/production/` | Production option-selection backfill and P&L replay |
| `backtest/vectorbt_trades/` | Replay of executed paper trades; optional VectorBT integration |
| `execution/` | Paper entries, position sizing, OCO monitoring, charges and exits |
| `data_manager/` | Market-data adapters and database clients, including SQL migrations |
| `common/` | Settings and shared models |
| `scripts/` | Daily workflows, feature calculations, calendar maintenance and backfills |
| `diagnostics/` | Miss analysis, diagnostic exports and data-quality checks |
| `dashboard.py` | Existing Research, Production and Trades views |
| `app.py`, `jobs.py`, `tasks.py` | HTTP API and allowlisted background tasks |

`analysis/strategy_registry.yaml` is the strategy source of truth. Its `SIGNAL` entries power production; `RESEARCH` entries remain available to research. No strategies were promoted or removed as part of extraction. `effective_prediction` remains the trading decision used downstream.

The news backend is independent. This service does not import `news_signal_service`, `src/news_sentiment`, news models or news scraping dependencies. Existing numeric macro/global-index features and the scheduled-event guard remain here because current strategies consume them. A future news-signal integration should use an explicit API or database contract.

The market and trading database is still the existing database. This extraction isolates application code, configuration and hosting; it does not split or migrate live tables. SQL needed by current clients is bundled under `data_manager/db/migrations`.

Current operational guidance is consolidated in [the runbook](docs/operations.md), with separate [Windows](docs/windows_scheduler.md) and [Linux](docs/linux_scheduler.md) scheduler instructions. Feature and schema notes are in [NIFTYFeatures.md](docs/NIFTYFeatures.md), [GlobalIndexLoader.md](docs/GlobalIndexLoader.md) and [DatabaseSchema.md](docs/DatabaseSchema.md).

## Install and run

Use Python 3.11 or newer; the container uses Python 3.12. From the Stockie repository root:

```powershell
python -m venv technical_analysis_service/.venv
technical_analysis_service/.venv/Scripts/python -m pip install "./technical_analysis_service[dev]"
Copy-Item technical_analysis_service/.env.example technical_analysis_service/.env
# Fill in the service's .env, or supply hosting environment variables.
technical_analysis_service/.venv/Scripts/python -m technical_analysis_service serve
```

The service listens at `http://127.0.0.1:5001`. Existing pages are `/research`, `/production` and `/trades`; `/health` is the process health check. Starting the server does not run market refresh, predictions or paper trades. An external scheduler or an API request starts each workflow.

For an extracted folder, run `pip install ".[dev]"` inside that folder, then `technical-analysis-service serve`. Installation includes all local code and YAML/SQL resources. Optional extras:

- `[research]`: VectorBT; the existing replay fallback remains available without it.
- `[azure]`: Azure SQL's Python ODBC client; the host also needs its ODBC driver. Supabase is the default and does not import ODBC.
- `[broker-login]`: Playwright and TOTP helpers for the optional interactive Kite token tool. Install Chromium with `python -m playwright install chromium` when using it.
- `[calendar]`: exchange calendars for calendar generation.
- `[dev]`: pytest.

`TECHNICAL_ANALYSIS_ENV_FILE` selects a service-specific environment file. Otherwise only this folder's `.env` is read; parent `.env` files are not discovered. Hosting environment variables take precedence. `TECHNICAL_ANALYSIS_OUTPUT_DIR` selects the writable artifact directory, including feature-store CSVs, reports and job records. Use an absolute persistent path when hosting; the default is this package's `output/` directory. Use absolute paths for optional local broker-token files as well.

For a hosted instance, set `TECHNICAL_ANALYSIS_API_KEY` and put the application behind HTTPS. All routes except `/health` then require `Authorization: Bearer <key>`. For browser dashboard access, use an authenticated reverse proxy that supplies that header. Without a key the service is intended for trusted localhost use.

## Hosting

Railway setup, the daily job roster and the news-score column are in [docs/railway.md](docs/railway.md). `python -m technical_analysis_service scheduler` (or `TECHNICAL_ANALYSIS_SCHEDULER=1` with `serve`) runs the roster in IST.

## Workflows and scheduling

```powershell
python -m technical_analysis_service tasks
python -m technical_analysis_service run research -- --help
python -m technical_analysis_service run research -- --start 2024-01-01 --end 2026-11-06
python -m technical_analysis_service run production.predict -- --start 2026-11-01 --end 2026-11-06
python -m technical_analysis_service run production.signal
python -m technical_analysis_service run paper.entry
python -m technical_analysis_service run paper.monitor
python -m technical_analysis_service run trades.replay -- --mode paper
```

`run <task> -- --help` shows the retained workflow's arguments. Existing cron schedules can be switched from the old script paths to these commands without changing their market timings. Keep the existing order: market/global/macro refresh and feature calculation, prediction, option snapshots/calculations and selection, then paper entry and monitoring. `production.signal` retains its existing prediction-plus-selection orchestration. Paper trading uses live quotes and writes simulated trades to the configured DB; it does not place broker orders.

No scheduler has been installed or changed by this refactor. Do not run a second copy of existing paper-entry/monitor schedules when cutting over. Coordinate cron and dashboard jobs with API jobs because external processes do not share the API job lock.

Maintenance and diagnostic tools remain explicit module commands, for example:

```powershell
python -m technical_analysis_service.diagnostics.analyze_precision_misses --help
python -m technical_analysis_service.diagnostics.daily_data_quality_check --help
python -m technical_analysis_service.scripts.backfill_NIFTY.backfill_nifty_option_pcr --help
```

Diagnostic commands are not part of the normal scheduled task roster. The Production page can still generate its miss reports on demand.

## HTTP contract

| Method and path | Result |
| --- | --- |
| `GET /health` | Existing process health response; does not probe credentials or the DB |
| `GET /api/v1/tasks` | Allowed workflow names |
| `GET /api/v1/strategies` | Every strategy's direction, type and definition |
| `POST /api/v1/jobs` | Submit a workflow; returns job record and poll URL, HTTP 202 |
| `GET /api/v1/jobs` | Most recent 100 jobs |
| `GET /api/v1/jobs/<id>` | State, timestamps, exit code and log tail |
| `GET /api/v1/production?start=YYYY-MM-DD&end=YYYY-MM-DD` | Existing production rows |
| `GET /api/v1/paper/trades?trade_date=YYYY-MM-DD` | Existing paper rows for a session |

Example job body:

```json
{"task":"research","args":["--start","2024-01-01","--end","2026-11-06"]}
```

Arguments are passed as individual strings to an allowlisted Python module, without a shell. Invalid requests return 400. The API allows one running job at a time to prevent concurrent artifact writes; another submission returns 409. Jobs report `running`, `done` or `failed`. Status and logs persist under `<output>/jobs`; this is a single-host job runner, not a distributed queue. Do not terminate the service during a running job: execution is owned by that process, and interrupted jobs are not automatically retried. If it is terminated, reconcile the running process and persisted job state before submitting another task. The original dashboard's background job controls retain their existing behavior.

## Container deployment

```powershell
docker build -t stockie-technical-analysis ./technical_analysis_service
docker run --rm -p 5001:5001 --env-file technical_analysis_service/.env -e HOST=0.0.0.0 -v stockie-technical-output:/data stockie-technical-analysis
```

Only the service directory is used as the build context. Secrets and generated outputs are excluded. The container runs under an unprivileged user and stores output on `/data/output`. Supply credentials through the host's secret manager or environment; no credentials were copied into this folder. Database connectivity, a funded quote subscription where required, and existing market-data/calendar tables remain runtime prerequisites.

## Compatibility and validation

The root `flask_app.py` is the supported direct launcher for the current dashboard and JSON API. It imports this service and defaults to the root `.env` and `output` for continuity. New standalone service commands use service-owned settings (see the runbook for pointing them at the root `.env`).

The root Flask command retains its historical port 5000. The independent service command uses port 5001 and adds the JSON API.

```powershell
python -m pytest technical_analysis_service/tests -q
python -m pytest -q
python -m pip wheel ./technical_analysis_service --no-deps
```

The service's regression tests use canonical imports and explicit sample settings, plus temporary artifacts for dashboard checks. DB-dependent tests remain opt-in with `RUN_DB_TESTS=1`. Extraction verification does not execute real predictions, refresh data or enter paper trades against your configured accounts.
