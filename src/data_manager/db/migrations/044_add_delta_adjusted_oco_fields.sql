ALTER TABLE "PaperExecutionSignal"
    ADD COLUMN IF NOT EXISTS entry_underlying_price double precision,
    ADD COLUMN IF NOT EXISTS entry_option_delta double precision;

ALTER TABLE "PaperOrder"
    ADD COLUMN IF NOT EXISTS oco_group_id varchar(100);
