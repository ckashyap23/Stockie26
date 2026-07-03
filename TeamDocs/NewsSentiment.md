# News Sentiment

News sentiment is persisted for research but is not currently consumed by the
production NIFTY cascade.

Daily pre-market sentiment for NIFTY research. Not wired into production
prediction yet.

## Flow

```text
RSS + NewsAPI
  -> article store
  -> FinBERT sentiment
  -> Azure OpenAI sector tags or keyword fallback
  -> NIFTY weighted composite
  -> Supabase + output/intelligence
```

## Daily Run

Render-safe command:

```bash
python scripts/daily_NIFTY/daily_news_sentiment.py --sector-classifier keyword --no-transformers
```

With Azure sector tagging:

```bash
python scripts/daily_NIFTY/daily_news_sentiment.py --sector-classifier llm --no-transformers
```

Specific date:

```powershell
python scripts/daily_NIFTY/daily_news_sentiment.py --target-date 2026-06-29 --sector-classifier keyword --no-transformers
```

## Backfill

```powershell
python scripts/backfill_NIFTY/backfill_news_sentiment.py --start-date 2026-06-25 --end-date 2026-06-29 --sector-classifier keyword --overwrite --no-transformers
```

## Env Vars

Required for useful ingestion:

```env
NEWSAPI_KEY=...
```

Hosted FinBERT:

```env
NEWS_SENTIMENT_SCORER=hf_finbert
HF_TOKEN=...
HF_INFERENCE_URL=https://router.huggingface.co/hf-inference/models/ProsusAI/finbert
```

Optional Azure sector tagging:

```env
AZURE_OPENAI_ENDPOINT=https://<resource>.cognitiveservices.azure.com
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT=<deployment-name>
AZURE_OPENAI_API_VERSION=2024-12-01-preview
```

Local FinBERT fallback for developer machines only (set env vars, model downloads automatically on first use):

```env
HF_FINBERT_FALLBACK=local_finbert
FINBERT_LOCAL_MODEL_PATH=models/ProsusAI/finbert
```

Do not enable local FinBERT fallback on 512 MiB Render cron.

## Output

```text
output/intelligence/news_articles/DD-MM-YYYY/news_articles.csv
output/intelligence/article_sentiment/DD-MM-YYYY/NIFTY_article_sentiment.csv
output/intelligence/market_sentiment/DD-MM-YYYY/NIFTY_market_sentiment.csv
```

DB tables:

```text
NewsArticle
NewsArticleSentiment
NiftyMarketSentiment
```
