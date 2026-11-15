# News and technical-analysis boundary

The independent `news_signal_service` owns event-based news ingestion, impact scoring, confidence, intensity and aggregation. See
[its documentation](../../news_signal_service/README.md).

The technical service does not import the news backend. The two share exactly one contract, a column set on `"NiftyPrediction"`:

| Column | Meaning |
|---|---|
| `news_score` | overnight macro news score in [-1, 1] for the trade date |
| `news_direction`, `news_confidence`, `news_run_id`, `news_scored_at` | direction, confidence, the run that produced it, when |

The news service writes the score for trade date D on the row whose `next_trade_date = D` (signal date + one trading day), from a window of
the previous trading day 15:30 IST to D 08:30 IST (migration `049_add_news_score.sql`). The Production page displays it per trade date.

News output does not change production CALL/PUT decisions: the prediction upsert never lists these columns (so re-running predictions cannot
overwrite them) and no strategy reads them. Using the score in a decision would need its own validation and a deliberate change.
