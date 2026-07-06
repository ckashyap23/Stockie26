ALTER TABLE "NiftyPrediction"
    ADD COLUMN IF NOT EXISTS watch_signal varchar(20),
    ADD COLUMN IF NOT EXISTS prior_watch_signal varchar(20),
    ADD COLUMN IF NOT EXISTS prior_watch_age smallint,
    ADD COLUMN IF NOT EXISTS promoted_prediction varchar(20),
    ADD COLUMN IF NOT EXISTS effective_prediction varchar(20),
    ADD COLUMN IF NOT EXISTS promotion_reason varchar(500);
