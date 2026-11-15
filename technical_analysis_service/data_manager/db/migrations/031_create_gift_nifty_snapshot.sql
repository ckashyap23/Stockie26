-- Migration 031: GiftNiftySnapshot — daily 9:15-9:20 AM IST candle for GIFT NIFTY
--
-- Each row is the single 5-minute candle that opens at 09:15 IST on a NIFTY
-- trading day. The close_920 value (price at 09:20 IST) is the primary signal
-- input: it captures where GIFT NIFTY was trading at the moment India's cash
-- market opened, giving a pre-open directional read before any domestic
-- strategy signal is generated.

CREATE TABLE IF NOT EXISTS "GiftNiftySnapshot" (
    trade_date   date             NOT NULL,
    open_915     double precision NOT NULL,
    high_920     double precision NOT NULL,
    low_920      double precision NOT NULL,
    close_920    double precision NOT NULL,
    fetched_at   timestamp        NOT NULL DEFAULT now(),
    source       varchar(50)      NOT NULL DEFAULT 'KITE_HISTORICAL_5M',
    CONSTRAINT pk_gift_nifty_snapshot PRIMARY KEY (trade_date)
);

COMMENT ON TABLE  "GiftNiftySnapshot" IS
    'Daily 9:15-9:20 AM IST candle for GIFT NIFTY (instrument_token=291849, exchange=NSEIX). '
    'close_920 = price at 9:20 AM IST — used as the pre-open NIFTY directional read.';
COMMENT ON COLUMN "GiftNiftySnapshot".open_915  IS 'GIFT NIFTY price at 9:15 AM IST (candle open).';
COMMENT ON COLUMN "GiftNiftySnapshot".close_920 IS 'GIFT NIFTY price at 9:20 AM IST (candle close) — primary signal input.';
