ALTER TABLE "PaperExecutionSignal"
    ADD COLUMN IF NOT EXISTS initial_target_move double precision,
    ADD COLUMN IF NOT EXISTS initial_stop_move double precision;
