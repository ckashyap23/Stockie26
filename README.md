# Stockie

The application is split into two independent backend services, each hostable on its own (see their READMEs).

| Location | Purpose |
| --- | --- |
| `flask_app.py` | Direct launcher for the current dashboard and technical-analysis API (port 5000) |
| `technical_analysis_service/` | Research strategies, production predictions, option selection, paper trading and the daily job roster |
| `news_signal_service/` | Overnight macro news score: seeded events -> headlines -> LLM judge -> one signed NIFTY score per trade date |
| `nifty_trigger_research/` | Retrospective research notebooks and spreadsheets (not part of either service) |

The two services share one contract: the news service writes `news_score` (plus direction/confidence) into the `NiftyPrediction` table on the row
whose `next_trade_date` is the trade date, and the dashboard shows it. The score does not influence predictions. See
[technical_analysis_service/docs/news_boundary.md](technical_analysis_service/docs/news_boundary.md).

Run the dashboard locally from this directory:

```powershell
.venv/Scripts/python.exe flask_app.py
```

Open http://127.0.0.1:5000. The launcher uses the root `.env` and `output/` locations. Its implementation lives in `technical_analysis_service`.
For a new environment install the technical backend with `python -m pip install ./technical_analysis_service`; its standalone server defaults to
port 5001. The [news backend](news_signal_service/README.md) has separate setup and dependencies.

## Hosting on Railway

Two services, each with its own Volume at `/data` and an in-process scheduler (no separate cron services):

| Service | Root Directory | Schedule (IST) | Guide |
| --- | --- | --- | --- |
| technical-analysis | `technical_analysis_service` | the daily job roster, 03:15-16:10, weekdays (`TECHNICAL_ANALYSIS_SCHEDULER=1`) | [docs/railway.md](technical_analysis_service/docs/railway.md) |
| news-signal | `news_signal_service` | ingest every 30 min; daily run 08:30 weekdays (`SCHEDULER_ENABLED=1`) | [docs/RAILWAY.md](news_signal_service/docs/RAILWAY.md) |

Disable the local Windows scheduled tasks before enabling the Railway scheduler, or paper trades will be placed twice.

## Tests

```powershell
.venv/Scripts/python.exe -m pytest technical_analysis_service/tests     # technical service
cd news_signal_service; python -m pytest                                # news service
```
