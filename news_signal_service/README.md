# NIFTY News Signal Service

Standalone backend API that turns overnight macro news into one signed NIFTY news score. It lives in
the Stockie26 repo but is **self-contained**: nothing under `app/` imports from `src/`, and it has its
own `requirements.txt`, `Dockerfile` and `railway.json`, so it deploys on its own.

```
seeds -> headlines -> dedupe -> seed match -> full text -> LLM judge -> aggregate -> (calibrate)
 (1)       (2a)        (2b)        (2c)          (3)           (4)            (5)          (6)
```

## What the output means

| Field | Meaning |
|---|---|
| `score` | signed, -1..1. Sign = indicated NIFTY direction. **An uncalibrated news score, not a forecast.** |
| `magnitude` | 0..1. Uncalibrated: `abs(score)`. After calibration: `min(abs(predicted_return) / training_scale, 1)`. See `magnitude_basis`. |
| `confidence` | 0..1 support for the interpretation. **Separate from magnitude; not a probability.** |
| `predicted_return` | fraction (previous close -> close of `target_date`). `null` until calibrated. |
| `groups` | per shock group score, plus `raw_sum` (what a naive sum would have given) |
| `evidence` | each judged event: headline, seed, direction, confidence, intensity, source count, `content_source`, `first_published`, rationale |

## Pipeline

1. **Seeds** - `data/event_seeds.csv` (61 events, one row each: aliases, priority, `LLM guidance`, `Shock group`, `Exclude cues`, optional `Context terms`). Includes oil price
   moves (E05), FII/FPI flows (R01), rupee moves (R02), global equity risk-off/rally (R03) and dollar/US-yield spikes (R04), and heavyweight listings/demergers/stake sales (C07).
2. **Headlines** - `data/feeds.yaml` (18 publishers / 21 feeds, P1/P2, `body: full|headline`, rights notes).
   Every run is recorded in a **timestamped archive** (`DATA_DIR/archive.sqlite3`): first publication, first
   ingestion, revisions. Only items first published by the 08:30 IST cutoff are used (window: 15:30 D-1 to 08:30 D).
   Then **dedupe** (embedding cosine + title Jaccard; the P1 source is the cluster representative) and
   **seed match** with a confidence (keyword title/summary hits + embedding similarity, context terms,
   an "unseeded high-impact" catch-all `U00`).
3. **Full text** - only for matched clusters. Per-site fetchers (`extract.py`), per-domain rate limit,
   URL-hash cache, `headline`-mode sources are never fetched. Falls back to the RSS summary.
4. **LLM judge** (`llm_judge.py`) - every matched event, every seed. The model reads the headline and article text plus the seed's
   hypothesis, guardrail and guidance and returns *relates to the seed?*, direction, size (intensity), confidence, surprise
   (beat/miss/in line) and whether it is commentary or already priced in. Anything that is not a clear, related, non-commentary,
   not-priced-in event scores exactly zero. See "LLM judge" below. There is no keyword/cue scoring path: cues are filters only (below).
5. **Aggregate** (`aggregate.py`): confidence is a **gate** (below `MIN_CONFIDENCE` -> 0), not a multiplier.
   Seeds sharing a `shock_group` are one shock: the k-th strongest counts `GROUP_DECAY**k`. Groups sum, then
   `score = tanh(raw / AGGREGATE_SCALE)`.
6. **Calibrate** (`app/calibration`): fits `return ~ score` on **all** sessions, walk-forward validated, and stores the
   training scale. Applied automatically when `DATA_DIR/calibration.json` exists.

## Cues are filters, never entry requirements

Relevance comes from similarity and direction/size from the LLM. Cues only REMOVE what is not a fresh event:
- **Per seed** (`Exclude cues` column of `event_seeds.csv`, matched on the headline as a word, phrase, or `prefix*`): e.g. G01 excludes
  "history", "1965", "cricket"; R02 excludes "Pakistani rupee"; E05 excludes "petrol price today", "olive oil". A match is logged in
  `matches.jsonl` as `excluded_by_cue:<cue>`.
- **Globally** (`not_a_fresh_event` in `commentary_filters.yaml`): history, explainers, "what to know", opinion columns, how-tos. Applied to
  each headline BEFORE dedupe (with the broker-call and stock-pick filters) so they cannot be scored or absorb real news into a cluster;
  every removal is logged in `commentary_dropped.jsonl`.
Seed matching looks at EVERY headline of a dedupe cluster and keeps the best link per seed, so a wrong merge cannot hide an event.
The headline cap (`MAX_ARTICLES_PER_RUN`, default 1500) is applied fairly over days and sources, not by dropping the earliest items.

## How an event becomes a score: the safeguards

1. **Importance, not similarity alone, decides which events get a judging slot** (`max_events_to_judge`, default 120):
   importance = similarity x (1 + 0.25 x ln(distinct outlets covering it)), P2 seeds x 0.8 (`similarity.ranking` in `tuning.yaml`).
   A 17-outlet story outranks a lone outlet with slightly higher similarity.
2. **One event = one full signal.** An event (dedupe cluster) judged under several seeds counts its strongest seed in full and each further
   seed at `EVENT_SECONDARY_WEIGHT` (default 0: strictly once), so the same article cannot enter through two seeds or shock groups.
3. **Source quality** (`data/source_quality.yaml`): social posts (0.3), self-published/newsletter (0.5) and raw-URL scrapers (0.5) multiply the
   LLM's confidence; low-weight sources fall under `MIN_CONFIDENCE` and score zero, and the more trustworthy headline represents an event.
4. **What the item IS comes first** (prompt v5). Before scoring, the model labels every item with an `event_status`: `occurred` (a decision,
   release or incident that has happened, with specifics), `market_move` (a completed, quantified move; valid only for the market-move seeds
   in `llm.market_move_seeds`: E05, R01-R04), `official_remarks` (one person's speech or interview), `scheduled_or_expected`,
   `opinion_or_forecast`, `wrap_or_preview` or `background` (a recap, or older than the window start; the prompt carries the window).
   Only `occurred` (or `market_move` on a market-move seed) scores. `official_remarks` score at most `llm.official_remarks_cap` (0.2) and never
   on a market-move seed. Everything else is zeroed and keeps its `event_status` as the status in the CSV. An item must also relate to the seed
   with `relation_confidence >= llm.min_relation_confidence` (0.7). An answer without a valid `event_status` is treated as unusable (unscored),
   never scored. `unverified` and `expectation` provenance still never score. Changing the prompt bumped `PROMPT_VERSION` to v5, so old
   cached answers are not reused (the cache key also includes the window).
   **Before any LLM call**, `commentary_filters.yaml` drops wraps, previews, live blogs, roundups and interviews by headline (`non_event`) and
   video/newsletter/photo pages by URL (`url_categories`); every drop is logged in `commentary_dropped.jsonl`.
5. **Heavyweight corporate events** have a seed (C07: listing, demerger, big stake sale, buyback) that needs a heavyweight named in the headline.

## Relevance and dedupe (benchmarked)

Relevance is a **similarity score**, not a keyword match: a headline is compared with each seed's short texts
(`event type`, `event type: alias` ...) and `score = similarity + 0.2 * keyword evidence`; it is relevant at `score >= gate`.
Keywords can lift a score but cannot make a headline relevant on their own. The same embedder drives dedupe (headline vs headline).

The model and recipe were chosen on `data/eval/relevance_golden.csv` (184 headlines: 62 real + 69 paraphrased positives with
acceptable seeds, 46 real irrelevant ones, 7 deliberately hard; the 5 newest seeds were added after the table below was measured, see the note under it). Re-run with `python -m app.eval.relevance_benchmark`.
Results (production seed-text recipe, keyword boost 0.2; best F1 for mpnet is at gate 0.46: precision 0.81, recall 0.67, 10% of irrelevant headlines pass):

| Method | top-1 seed correct | top-3 | AUROC | best F1 |
|---|---|---|---|---|
| keyword gate (previous) | 0.42 | 0.52 | 0.74 | 0.44 |
| TF-IDF | 0.48 | 0.68 | 0.80 | 0.46 |
| FinBERT-base vectors | 0.38 | 0.55 | 0.60 | 0.38 |
| MiniLM-L6 | 0.65 | 0.84 | 0.90 | 0.63 |
| bge-small | 0.66 | 0.83 | 0.84 | 0.62 |
| **all-mpnet-base-v2 (default)** | **0.79** | **0.91** | **0.90** | **0.73** |

With the 5 new seeds (60 total) and the golden set extended to match, mpnet measures top-1 0.82, top-3 0.96, AUROC 0.92, best F1 0.78 at
gate 0.446. Treat that as optimistic: the new seeds' test headlines include the very headlines (8-10 Sep 2026) used to design them.

FinBERT-base is a sentiment model: its vectors do not separate topics (AUROC near chance without keywords), so it is not used for
similarity. Known limits: the golden set is small and labelled by hand (review it); precision is not high at the recall-oriented gate
(0.35), so the downstream judge must be able to veto an irrelevant match; on a live run about half of all events pass 0.35
(172 of 306), 84 pass 0.45 and 30 pass 0.55, and `max_events_to_judge` (60) then keeps the strongest. Dedupe uses 0.74 on dense vectors
(read off sampled pairs: same-event pairs sit at 0.78+, different events at 0.73 and below).

Docker installs the CPU build of torch + `requirements-ml.txt` and bakes the model in (not yet built or deployed). If the model
cannot load, the run falls back to TF-IDF and says so in `warnings`.

## Backfill: run the pipeline over past dates

```bash
python -m app.backfill.run --from 2026-09-09 --to 2026-11-09 --embedder sentence_transformers --llm-provider gemini [--resume] [--limit 3]
```

Output goes to `var/backfill/` (separate archive, so synthetic history never mixes with live runs): `backfill_scores.csv` (one row per
weekday: score, direction, confidence, news items, matched events, LLM judged/vetoed/unscored, top groups, top headline, run id),
`signals/<date>.json`, and the full per-stage audit folder `runs/<date>/<HHMMSS>/` for every date. It is resumable (`--resume` redoes only
`partial` dates; LLM answers are cached) and stops if the LLM quota looks exhausted.

RSS has no history, so past headlines come from date-filtered Google News searches (`data/backfill_queries.yaml`, editable). Read the scores
with these limits in mind:
- **Date granularity only.** Historical items carry a date, not a time. Each target date uses the items dated from the previous trading day
  through the day before it (a Monday uses Fri/Sat/Sun); nothing dated the target day is used, so there is no look-ahead, but pre-08:30
  news on the day itself is lost and the previous day's morning news (already priced in) is included.
- **Headlines only.** Google links cannot be resolved to article bodies, so every event is judged on its headline (live runs also read the body).
- **Partial coverage** (each query returns its most relevant ~15-60 items) and **weekdays include exchange holidays** (not modelled).
- **The LLM matters.** The same date judged by Groq `gpt-oss-120b` and by Gemini `gemini-3.1-flash-lite` gave different scores (-0.09 vs +0.06 on
  2026-10-05); the model used is recorded in each run's `meta.json`.

## Editable configuration

All hand-edited files live in one folder (`CONFIG_DIR`, default `data/`). Nothing else needs a code change.

| File | What you control |
|---|---|
| `event_seeds.csv` | the 61 seeds: aliases, priority, LLM guidance, shock group, exclude cues, context terms |
| `feeds.yaml` | publishers and feeds: on/off, P1/P2, full-text vs headline-only, rights notes (`/v1/sources` is generated from it) |
| `nifty_heavyweights.yaml` | company gate: which companies, approximate index weights, which seeds are gated |
| `commentary_filters.yaml` | headline and URL patterns (broker calls, stock picks, wraps/previews/live blogs, non-event pages) dropped before the LLM and logged |
| `tuning.yaml` | LLM batching and intensity caps, similarity gates, unseeded catch-all, keyword weights |
| `.env` | thresholds and switches (`MIN_CONFIDENCE`, `GROUP_DECAY`, window times, scorer ...) |

Run `python -m app.validate_config` after editing: it reports errors (bad regex, unknown seed IDs, an exclude cue that is
also an alias, duplicate feed URLs, bad priorities) and warnings (an alias shared by two seeds).
Seeds, heavyweights, filters and tuning are re-read on every run, so edits apply without a restart.
Every run records the hash of each config file in its `meta.json`.

**On Railway** set `CONFIG_DIR=/data/config`: the defaults are copied there on first start (existing files are never
overwritten), so edits made on the Volume survive redeploys. Without it, edits are committed to git and redeployed.

## Stock-pick handling

- **Broker calls / stock-pick lists are dropped** before seed matching (`data/commentary_filters.yaml`, headline regexes,
  editable). Every dropped item is logged to `runs/<date>/<HHMMSS>/commentary_dropped.jsonl` with the pattern, category and the
  seeds it *would* have matched, so you can monitor what the filter costs.
- **Company-level seeds (C01, C02, C04) need a NIFTY heavyweight named in the headline** (`data/nifty_heavyweights.yaml`, editable,
  approximate weights) and intensity is scaled by `min(1, weight% / full_weight_pct)`. C03 (sector-wide updates) and C05 are not gated.

## Per-run audit trail

Every persisted run writes `DATA_DIR/runs/<date>/<HHMMSS>/` (never overwritten; reruns get their own folder):
`meta.json` (settings snapshot without secrets, seed/feed file hashes, stage timings), `headlines.jsonl`, `clusters.jsonl`,
`matches.jsonl` (every candidate seed incl. rejected, with scores), `fulltext.jsonl`, `judgements.jsonl` (LLM details,
direction/confidence/intensity), `llm_calls.jsonl` (every prompt and raw answer), `aggregation.json` (gate, rank and decay per cluster), `signal.json`. About 0.8 MB per run.
The response carries `run_id`. `persist:false` writes nothing. There is no retention/pruning yet.

## API

All `/v1` routes need header `X-API-Key` (keys from env `API_KEYS`). `/health` is open. Docs at `/docs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness (Railway healthcheck) |
| POST | `/v1/ingest` | fetch feeds into the archive (run every ~30 min) |
| POST | `/v1/signal/run` | start a run: 202 + job (poll `/v1/runs/{id}`); `"wait": true` blocks. Body: `{"target_date": "2026-01-05", "replay": false, "cutoff_time": "09:00"}`; 409 if a run is active |
| GET | `/v1/runs`, `/v1/runs/{job_id}` | job status |
| GET | `/v1/signal?target_date=` | read a stored signal exactly as saved (latest if omitted) |
| GET | `/v1/signal/{date}?include=scored\|relevant\|all` | final score + every seed with its score and deduplicated articles |
| GET | `/v1/signal/{date}/csv?view=articles\|seeds` | CSV download |
| GET | `/v1/archive/stats` | archive size and calibration state |
| GET | `/v1/events` | seed events with exclude-cues/shock-group (`?family=`) |
| GET | `/v1/sources` | the trimmed publisher registry |

`replay: true` rebuilds the signal from the archive exactly as it stood at the cutoff (no live fetch) - the
basis for backtests.

## Run locally

```bash
cd news_signal_service
pip install -r requirements-dev.txt
cp .env.example .env          # ALLOW_UNAUTHENTICATED=true for local only, or API_KEYS=dev
uvicorn app.main:app --reload
python -m pytest              # Windows: add --basetemp=<writable dir> if tmp_path is denied
```

## LLM judge

Set a provider and key in `.env` (no code change), then verify with `python -m app.llm.check`:

| Provider | `LLM_PROVIDER` | Key variable | Notes |
|---|---|---|---|
| Google Gemini (free tier) | `gemini` (default) | `GEMINI_API_KEY` | key from aistudio.google.com; free-tier quotas and models change, see your AI Studio limits |
| Groq (free tier) | `groq` | `GROQ_API_KEY` | key from console.groq.com |
| OpenAI | `openai` | `OPENAI_API_KEY` | |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | default model `claude-haiku-5-5` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | has free models |
| any OpenAI-compatible endpoint | `openai_compat` | `LLM_API_KEY` | also set `LLM_BASE_URL` and `LLM_MODEL` |

`LLM_MODEL` overrides the provider's default model id (ids change often: always check with `app.llm.check`). Defaults tested on
2026-11-09: Groq `openai/gpt-oss-120b` and Gemini `gemini-3.1-flash-lite` (32/33 on the golden set each); Gemini's larger Flash models
were slow or overloaded that day, and Groq's `llama-3.3-70b-versatile` no longer exists.
Built for free tiers: events are batched (`llm.batch_size`), calls are paced (`LLM_MIN_INTERVAL_SECONDS`, default 4 s), 429/5xx are
retried with backoff, at most `LLM_MAX_CALLS_PER_RUN` (40) requests are made per run, and every answer is cached
(`DATA_DIR/cache/llm_judgements.sqlite3`): a rerun, a replay, or the same headline on the next day costs no call and gives the
identical answer. If no key is set, or a call fails, the affected events score zero and the run says exactly how many and why;
nothing is silently replaced by a weaker method. Article text is sent to the provider: do not enable it for sources whose terms forbid that.

Judge quality: `python -m app.eval.judge_benchmark` scores the configured model on `data/eval/judge_golden.csv` (33 hand-labelled
cases incl. the failures we saw: "RBI cuts" misread as negative, broker calls, tiny-bank updates, CEO commentary). Edit that file.
Changing the prompt (`app/llm/prompt.py`) means bumping `PROMPT_VERSION` so cached answers are not reused.

## Calibrating

```bash
python -m app.calibration.fit --prices nifty_close.csv   # columns: date, close - every trading session
```

Needs >= 60 sessions that have both a stored score and a return. Build history by running the service daily or by
replaying the archive. `data/eval/labelled_sessions.csv` (164 sessions) has no quiet days and must not be the only
calibration set. Target: previous close -> close.

## Deploy on Railway

1. New Service -> Deploy from the Stockie26 GitHub repo.
2. Service Settings -> **Root Directory = `news_signal_service`**; Railway picks up `railway.json` + `Dockerfile`.
3. Variables: `API_KEYS`, `LLM_PROVIDER` + its key (e.g. `GEMINI_API_KEY`), optionally `NEWSAPI_KEY`, `CONFIG_DIR`.
4. Attach a **Volume** at `/data` - it holds the article archive, body cache, stored signals and calibration. Without
   it history is lost on every deploy and calibration is impossible.
5. Two cron services (`python -m app.trigger ingest` every 30 min; `python -m app.trigger run` at 03:30 UTC = 09:00 IST).
   Window: previous weekday 16:00 -> target day 09:00 IST. Full guide: [docs/RAILWAY.md](docs/RAILWAY.md).

## Known limits

- The exclude-cue lists are hand-written starting points; tune them against `commentary_dropped.jsonl` and `matches.jsonl`.
- The LLM can only use what the article says about consensus ("priced in"); headline-only sources give it little.
- Every matched event now costs LLM quota (about 15-20 calls per run at batch size 8): see `LLM_MAX_CALLS_PER_RUN`.
- The LLM judge is only as good as the model and the article text it sees (paywalled sources give headline + summary only);
  its `intensity` is an uncalibrated size judgement. FinBERT is not used for judging; it only provides the baseline columns in `var/backfill/news_vs_seed_scores.csv` (`app.backfill.consolidate`).
- TF-IDF dedupe/matching is lexical; set `EMBEDDER=sentence_transformers` for paraphrases.
- Publisher rights are unresolved for most sources; `S11` (Google News for Reuters/AP/WSJ, headline-only) is enabled by owner decision; its terms of use are unreviewed.

