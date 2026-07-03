-- Migration 015: Rename NiftyPrediction.trade_date -> signal_date
-- Clarifies that this column is the NIFTY market observation/signal date,
-- NOT the execution date. next_trade_date is the execution date.
-- Idempotent: wrapped in DO block, only renames if column still called trade_date.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'NiftyPrediction' AND column_name = 'trade_date'
    ) THEN
        ALTER TABLE "NiftyPrediction" RENAME COLUMN trade_date TO signal_date;
        RAISE NOTICE 'Renamed NiftyPrediction.trade_date to signal_date';
    ELSE
        RAISE NOTICE 'NiftyPrediction.signal_date already exists, skipping rename';
    END IF;
END$$;
