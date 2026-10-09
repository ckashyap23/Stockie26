-- Preserve both morning partial snapshots and final daily closes.
DROP INDEX IF EXISTS ux_global_index_ohlc_code_date;

COMMENT ON COLUMN "GlobalIndexOhlc".is_final IS
    'True for a completed daily bar; false for a point-in-time morning snapshot.';

COMMENT ON COLUMN "GlobalIndexOhlc".source IS
    'Data granularity/source. Different sources may coexist for one index and date.';
