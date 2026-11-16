# Hosting on Railway

Two services, each from this repository with its own Root Directory and Volume:

| Service | Root Directory | Runs | Volume |
|---|---|---|---|
| **technical-analysis** | `technical_analysis_service` | dashboard/API (`serve`) **and**, with `TECHNICAL_ANALYSIS_SCHEDULER=1`, the whole daily job roster | `/data` |
| **news-signal** | `news_signal_service` | news API **and**, with `SCHEDULER_ENABLED=1`, the 30-minute ingest and the 08:30 IST daily run | `/data` |

Railway cron services run in UTC, one command each, and every one would rebuild this image, so the roster below is run by an in-process
scheduler in IST instead (`railway_scheduler.py`). It only decides *when*; each firing starts `scheduled_jobs --job <id>`, which already
provides the per-job time windows, the NSE-holiday check, same-day dependency checks (prediction waits for open-gap, etc.), redacted logs
and status files under `/data/output/scheduler/`.

## Daily roster (IST, Monday-Friday; NSE holidays are skipped by the job runner)

| Time | Job | Workflow |
|---|---|---|
| 03:15 | us-index | `market.global --mode us-eur` |
| 07:30 | kite-token | headless Kite login (needs `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`) |
| 08:30 | option-instruments | `options.instruments` |
| 09:00 | macro-refresh | `market.macro` |
| 09:05 | asia-partial | `market.global --mode asia-partial` |
| 09:11 | gift-preopen | `market.gift --mode pre-open` |
| 09:15 | option-snapshot | `options.snapshot --snapshot-label OPEN_0915` |
| 09:20 | gift-open | `market.gift --mode open` |
| 09:22 | open-gap | `market.open-gap` |
| 09:24 | prediction | `production.signal` |
| 09:27 | paper-entry | `paper.entry` |
| 09:32 | morning-email | `daily_morning_email`: one email with the day's prediction, news score, option and paper trade, news CSV attached |
| 09:30-15:25 every 5 min, 15:29 | paper-monitor | `paper.monitor` |
| 15:15 | gift-close | `market.gift --mode close` |
| 15:32 | paper-report | `paper.report` |
| 15:45 | asia-final | `market.global --mode asia-final` |
| 15:47 | market-refresh | `market.refresh` (also creates the next trade date's prediction row) |
| 16:00 | option-ohlc | `options.ohlc` |
| 16:10 | signal-summary | `daily_signal_summary` |

The source of truth is `JOBS` in `scheduled_jobs.py`; `python -m technical_analysis_service.scheduled_jobs --workspace /data --status` shows the
latest state of each job.

News (separate service): ingest every 30 minutes, every day; the daily run at **08:30 IST** Monday-Friday for trade date D over the window
*previous weekday 15:30 -> D 08:30*; it publishes the score to `NiftyPrediction.news_score` (below). This finishes before the 09:24 prediction;
the prediction does not read it.

## One-time setup

1. **Disable the Windows scheduled tasks first** (all 17 `Stockie` tasks). Running both schedulers would place duplicate paper trades.
2. Create the service from this repo, Root Directory `technical_analysis_service`; Railway reads `railway.json` and the `Dockerfile`.
3. Add a **Volume at `/data`**. Set `RAILWAY_RUN_UID=0` so the non-root container user can write to it.
4. Variables (values from your local `.env`; never commit it). Everything else has a built-in default; `.env.example` lists the rest.

   | Variable | Value |
   |---|---|
   | `DATABASE_PROVIDER`, `SUPABASE_CONN_STR` | `supabase`, the connection string |
   | `KITE_API_KEY`, `KITE_API_SECRET` | from your Kite app |
   | `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET` | for the unattended token login |
   | `KITE_REDIRECT_HOST`, `KITE_REDIRECT_PORT` | `127.0.0.1`, `5001`: must match the redirect URL registered in the Kite app (the default port is 5000) |
   | `NIFTY_TARGET_PCT`, `TARGET_PCT_EFFECTIVE`, `SL_PCT_EFFECTIVE` | `0.006`, `0.006`, `0.004` (no default; `NIFTY_TARGET_PCT` defaults to 0.010, which would change the labels) |
   | `RAILWAY_RUN_UID` | `0` (Volume write access) |
   | `TECHNICAL_ANALYSIS_API_KEY` | a random string, **or** leave unset and do not generate a public domain. With no key every route except `/health` returns 503 (the scheduler is unaffected); a browser cannot send the `Authorization: Bearer` header, so use curl or a proxy |
   | `EMAIL_PROVIDER`, `RESEND_API_KEY`, `EMAIL_FROM`, `NOTIFY_EMAIL_TO` | `resend`, your Resend key, a sender on a domain verified in Resend, the recipient(s) |
   | `NEWS_SERVICE_URL`, `NEWS_API_KEY` | the news service's private address (`http://<news service name>.railway.internal:8080`) and one of its `API_KEYS`; used to attach the news-articles CSV |
   | `TECHNICAL_ANALYSIS_SCHEDULER` | `1`, set last, after the Windows tasks are disabled |

   Do not copy the local `KITE_LOGIN_HEADLESS=0` (the image and the kite-token job force headless) or `TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED`.
5. Deploy; check `/health`, then watch the logs at 07:30 IST for `kite-token`.
6. Memory: the image includes Chromium (for the token login); give the service roughly 2 GB.

## Morning email

The `morning-email` job sends the only email, at 09:32 IST (after prediction at 09:24 and paper entry at 09:27, so the paper trade exists). Subject:
`Stockie <trade date> | NIFTY <CALL|PUT|NO TRADE> | News <score>`. The body has the selected option and, when there is a trade, the paper trade
(lots x lot size, entry price, amount, target and stop-loss prices with their NIFTY-move percentages). The CSV attachment is fetched from the news
service (`/v1/signal/<date>/csv`): one row per article and seed with the Gemini direction, intensity, score and rationale. A missing news score, paper
trade or attachment is stated in the email, never a reason for it not to be sent. Test without sending: `python -m technical_analysis_service.scripts.daily_NIFTY.daily_morning_email --dry-run`.
The news service sends no email.

## News score in the prediction table

`NiftyPrediction` has `news_score`, `news_direction`, `news_confidence`, `news_run_id`, `news_scored_at` (migration `049_add_news_score.sql`; the
columns are also created on demand). The score for trade date D is stored on the row whose `next_trade_date = D` (signal date + one trading day).
The prediction upsert never lists these columns, so re-running predictions cannot overwrite them, and nothing in the strategy logic reads them.
The Production page shows `news_score` and `news_direction` next to each trade date's prediction and actual label.

Set `NEWS_SCORE_DB_URL` (or `SUPABASE_CONN_STR`) on the news service to the same database. If the signal row for D does not exist yet the score is
not written (the run reports it); `python -m app.publish --date D` or `--csv var/backfill/backfill_scores.csv` fills it later.

## Not verified

Not deployed yet: the image build (Chromium), the headless Kite login on Railway, and the volume permissions are untested there. The scheduler,
roster timing and publish SQL are covered by unit tests only.
