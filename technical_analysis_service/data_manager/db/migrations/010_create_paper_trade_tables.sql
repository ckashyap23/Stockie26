CREATE TABLE IF NOT EXISTS "PaperExecutionSignal" (
    id                         bigserial PRIMARY KEY,
    symbol                     varchar(50) NOT NULL DEFAULT 'NIFTY',
    model_version              varchar(50) NOT NULL DEFAULT 'cascade_v1',
    signal_trade_date          date NOT NULL,
    paper_trade_date           date NOT NULL,
    paper_platform             varchar(30) NOT NULL DEFAULT 'STOCKIE',
    direction                  varchar(20),
    selected_strategy          varchar(50),
    prediction_strategy        varchar(120),
    option_symbol              varchar(120) NOT NULL,
    option_token               bigint NOT NULL,
    option_type                varchar(10),
    quantity                   integer NOT NULL DEFAULT 1,
    lot_size                   integer,
    planned_entry_price        double precision,
    target_1_price             double precision,
    target_2_price             double precision,
    stop_loss_price            double precision,
    status                     varchar(30) NOT NULL DEFAULT 'PLANNED',
    source_selection_trade_date date NOT NULL,
    error_message              text,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_paper_execution_signal UNIQUE
        (symbol, model_version, signal_trade_date, paper_trade_date, paper_platform)
);

CREATE INDEX IF NOT EXISTS ix_paper_execution_signal_due
    ON "PaperExecutionSignal" (paper_trade_date, status, symbol);

CREATE TABLE IF NOT EXISTS "PaperOrder" (
    id                         bigserial PRIMARY KEY,
    paper_execution_signal_id  bigint NOT NULL REFERENCES "PaperExecutionSignal"(id),
    paper_platform             varchar(30) NOT NULL DEFAULT 'STOCKIE',
    order_role                 varchar(20) NOT NULL,
    side                       varchar(10) NOT NULL,
    order_type                 varchar(20) NOT NULL DEFAULT 'MARKET',
    quantity                   integer NOT NULL,
    requested_price            double precision,
    filled_price               double precision,
    status                     varchar(30) NOT NULL,
    payload_json               jsonb,
    error_message              text,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_paper_order_signal
    ON "PaperOrder" (paper_execution_signal_id, order_role, status);

CREATE TABLE IF NOT EXISTS "PaperTradeResult" (
    id                         bigserial PRIMARY KEY,
    paper_execution_signal_id  bigint NOT NULL UNIQUE REFERENCES "PaperExecutionSignal"(id),
    entry_price                double precision,
    entry_time                 timestamptz,
    current_price              double precision,
    current_quote_time         timestamptz,
    exit_price                 double precision,
    exit_time                  timestamptz,
    exit_reason                varchar(50),
    pnl_points                 double precision,
    pnl_per_lot                double precision,
    return_pct                 double precision,
    status                     varchar(30) NOT NULL DEFAULT 'OPEN',
    source                     varchar(30) NOT NULL DEFAULT 'STOCKIE',
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_paper_trade_result_status
    ON "PaperTradeResult" (status, current_quote_time);

CREATE TABLE IF NOT EXISTS "PaperTradeEvent" (
    id                         bigserial PRIMARY KEY,
    paper_execution_signal_id  bigint NOT NULL REFERENCES "PaperExecutionSignal"(id),
    event_time                 timestamptz NOT NULL DEFAULT now(),
    event_type                 varchar(50) NOT NULL,
    price                      double precision,
    message                    text,
    payload_json               jsonb
);

CREATE INDEX IF NOT EXISTS ix_paper_trade_event_signal
    ON "PaperTradeEvent" (paper_execution_signal_id, event_time);
