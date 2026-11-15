-- Migration 027: add is_final to GlobalIndexOhlc; change conflict key to (index_code, trade_date)
--
-- Background: the old loader used yf.download(interval="1d") which only returns closed sessions.
-- The new 3-tier loader also stores partial intraday data (is_final=FALSE, source="yfinance_5m").
-- We need a unique index on (index_code, trade_date) so subsequent upserts can promote a partial
-- row to final when the session closes, without leaving stale duplicates.

-- 1. Rename legacy source values for consistency.
UPDATE "GlobalIndexOhlc" SET source = 'yfinance_1d' WHERE source = 'yfinance';

-- 2. Add is_final column (DEFAULT true so all existing final/completed rows are correct).
ALTER TABLE "GlobalIndexOhlc" ADD COLUMN IF NOT EXISTS is_final boolean NOT NULL DEFAULT true;

-- 3. Deduplicate: for any (index_code, trade_date) pair that already has more than one row
--    (shouldn't happen with the old single-source loader, but safety first),
--    keep only the row with the latest fetched_at.
DELETE FROM "GlobalIndexOhlc" a
USING "GlobalIndexOhlc" b
WHERE a.index_code = b.index_code
  AND a.trade_date = b.trade_date
  AND a.fetched_at < b.fetched_at;

-- 4. Create the unique index used as the upsert conflict target.
--    The old PK (index_code, trade_date, source) is left in place; the new unique index
--    is the effective deduplication key going forward.
CREATE UNIQUE INDEX IF NOT EXISTS ux_global_index_ohlc_code_date
    ON "GlobalIndexOhlc" (index_code, trade_date);

COMMENT ON COLUMN "GlobalIndexOhlc".is_final IS
    'True when this row represents a fully-closed trading session (source=yfinance_1d). '
    'False for partial intraday snapshots (source=yfinance_5m) taken while the market is still open.';

COMMENT ON COLUMN "GlobalIndexOhlc".source IS
    'yfinance_1d  = completed daily bar from yf.download(interval="1d").'
    'yfinance_5m  = partial OHLC reconstructed from intraday 5-minute bars.';
