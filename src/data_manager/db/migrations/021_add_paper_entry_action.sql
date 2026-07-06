ALTER TABLE "PaperExecutionSignal"
    ADD COLUMN IF NOT EXISTS source_final_prediction varchar(20),
    ADD COLUMN IF NOT EXISTS promoted_prediction varchar(20),
    ADD COLUMN IF NOT EXISTS signal_day_close_1515 double precision,
    ADD COLUMN IF NOT EXISTS entry_action varchar(40),
    ADD COLUMN IF NOT EXISTS opening_gap_pct double precision,
    ADD COLUMN IF NOT EXISTS call_reclaim_level double precision;
