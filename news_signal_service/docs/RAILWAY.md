# Hosting on Railway

One service. The web process serves the API and, with `SCHEDULER_ENABLED=1`, runs its own schedule (`app/scheduler.py`), so no separate cron
services are needed:

| What | When (IST) | Does |
|---|---|---|
| ingest | every 30 min, every day (weekends too, so Monday's window has the weekend) | fetch all feeds into the archive |
| daily run | **08:30**, Monday-Friday | score trade date D over the window *previous weekday 15:30 -> D 08:30* and publish it to `NiftyPrediction.news_score` |

The run takes a few minutes (dedupe, ~25 LLM calls), so it starts at 08:30 and is ready before the technical jobs at 09:00-09:24.
Exchange holidays are not modelled: a run on a holiday still scores but publishes nothing (no prediction row has that trade date).

## One-time setup

1. New service from the repo, **Root Directory = `news_signal_service`** (Railway reads `railway.json` and the `Dockerfile`; one uvicorn worker on purpose).
2. Attach a **Volume at `/data`** (archive, run logs, LLM cache, stored signals, optional config).
3. Variables:

| Variable | Value |
|---|---|
| `SCHEDULER_ENABLED` | `1` |
| `LLM_PROVIDER` + key | e.g. `gemini` + `GEMINI_API_KEY` (check with `railway run python -m app.llm.check`) |
| `NEWS_SCORE_DB_URL` | the technical database connection string (same as its `SUPABASE_CONN_STR`) |
| `RAILWAY_RUN_UID` | `0` (Volume write access) |
| `API_KEYS` | optional: long random strings (callers send `X-API-Key`). Unset, every `/v1` route returns 503 but the scheduler still runs and publishes |
| `NEWSAPI_KEY` | optional extra source |
| `CONFIG_DIR` | optional `/data/config` to keep edits to seeds/feeds across deploys (default: the copy in the image) |

Everything else has a default: `DATA_DIR=/data` comes from the Dockerfile, the window is 15:30 to 08:30, the run is 08:30, the embedder is
`sentence_transformers` (model baked into the image). Do not copy `ALLOW_UNAUTHENTICATED`, `HF_TOKEN`, `HF_INFERENCE_URL` or `NEWS_SENTIMENT_SCORER` from a
local `.env` (the last three belong to a removed scorer).

4. Memory: the similarity model needs about 2 GB (unverified). Deploy, then check `GET /health`, `GET /v1/sources` and `POST /v1/ingest`.

## The window

For trade date D the run reads everything **first published between the previous weekday 15:30 IST and D 08:30 IST** (Monday: Friday 15:30, so
the weekend is included; `WINDOW_PREVIOUS_DAY=calendar_day` gives a literal "yesterday 15:30"). It reads this from the archive plus a fresh
fetch; news published after the cutoff is ignored. A run at 08:30 sees only what was ingested up to then, which is why the 30-minute ingest matters.

## Where the score goes

On a successful non-replay run the score is written to the technical database's `NiftyPrediction` row whose `next_trade_date = D`
(`news_score`, `news_direction`, `news_confidence`, `news_run_id`, `news_scored_at`). The technical prediction logic never reads it.
If the row does not exist yet, nothing is written and the job result says so; fill it later with:

```bash
python -m app.publish --date 2026-11-12                                  # one stored live signal
python -m app.publish --csv var/backfill/backfill_scores.csv             # a backfill
```

## Using it manually

```bash
curl -X POST "$URL/v1/signal/run" -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"target_date":"2026-11-10"}'   # 202 + job
curl "$URL/v1/runs/<job_id>" -H "X-API-Key: $KEY"
curl "$URL/v1/signal/2026-11-10" -H "X-API-Key: $KEY"                    # final score + every seed with its deduplicated articles
curl -OJ "$URL/v1/signal/2026-11-10/csv" -H "X-API-Key: $KEY"            # CSV (?view=seeds for one row per seed)
```

`"cutoff_time":"08:00"` moves the window end for that run; `"replay":true` rebuilds from the archive as it stood at the cutoff (and does not
publish); `"wait":true` blocks and returns the signal. Only one run at a time: a second trigger gets **409**. `python -m app.trigger run|ingest` does
the same from outside (needs `SERVICE_URL`, `TRIGGER_API_KEY`) if you prefer Railway cron services over the built-in scheduler.

## What to watch

- **LLM quota.** About 20-25 calls per run; a free tier can run out (the run reports how many events were not scored).
- **Restarts.** A run interrupted by a restart is marked failed; trigger it again.
- **Not deployed yet.** Built and unit-tested locally only; expect to adjust on the first deploy.
- **Archive growth.** Nothing prunes `archive.sqlite3` or old run folders yet.
