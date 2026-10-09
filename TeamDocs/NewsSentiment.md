# News Sentiment

News sentiment is maintained as research context. It is not currently consumed by
the production NIFTY cascade.

There is no active NIFTY daily cron or historical backfill wrapper for news
sentiment. The core subsystem remains available for explicit research use only.

## Scope

- Article ingestion and market sentiment rows can be refreshed independently.
- Sentiment backtests can compare residual returns against technical-analysis
  predictions.
- Any production integration should be added as an explicit model layer rather
  than silently changing the current cascade.
- Current production predictions are driven only by technical/global/open-gap
  features and the configured production `SIGNAL` strategies.
