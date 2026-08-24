-- Migration 039: add force_exit_time to NiftyOptionSelection.
--
-- DRIFT_PROBE_PUT requires an intraday force-exit at 13:00 IST rather than
-- the default end-of-day 15:15 exit. This column stores the strategy-specific
-- force-exit time. NULL = use the pipeline default (15:15 IST).

ALTER TABLE "NiftyOptionSelection"
    ADD COLUMN IF NOT EXISTS force_exit_time time;

COMMENT ON COLUMN "NiftyOptionSelection".force_exit_time
    IS 'Optional intraday force-exit time (IST). NULL = pipeline default (15:15). DRIFT_PROBE_PUT sets 13:00.';
