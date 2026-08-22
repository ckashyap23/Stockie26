-- ============================================================
-- Migration 001: Trading Signal Engine + Option Selector Tables
-- Target: Azure SQL Server (dbo schema)
-- Safe to run multiple times (idempotent).
-- Does NOT drop or alter any existing table.
-- ============================================================

-- ============================================================
-- PHASE 1: DB Foundation
-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'TradingCalendar' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.TradingCalendar (
        calendar_date       DATE            NOT NULL,
        exchange            VARCHAR(20)     NOT NULL CONSTRAINT df_tc_exchange DEFAULT 'NSE',
        is_trading_day      BIT             NOT NULL,
        is_weekly_expiry    BIT             NOT NULL CONSTRAINT df_tc_weekly DEFAULT 0,
        is_monthly_expiry   BIT             NOT NULL CONSTRAINT df_tc_monthly DEFAULT 0,
        is_special_session  BIT             NOT NULL CONSTRAINT df_tc_special DEFAULT 0,
        notes               VARCHAR(300)    NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_tc_created DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2       NULL,

        CONSTRAINT pk_trading_calendar PRIMARY KEY (calendar_date, exchange)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_trading_calendar_trading_day' AND object_id = OBJECT_ID('dbo.TradingCalendar'))
    CREATE INDEX ix_trading_calendar_trading_day
    ON dbo.TradingCalendar (exchange, is_trading_day, calendar_date);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WatchedInstrument' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.WatchedInstrument (
        watched_id          BIGINT          NOT NULL IDENTITY(1,1),
        tradingsymbol       VARCHAR(50)     NOT NULL,
        exchange            VARCHAR(20)     NOT NULL,
        name                VARCHAR(200)    NULL,
        instrument_token    BIGINT          NULL,
        segment             VARCHAR(50)     NULL,
        tick_size           FLOAT           NULL,
        lot_size            INT             NULL,
        instrument_type     VARCHAR(30)     NOT NULL,
        sector              VARCHAR(100)    NULL,
        industry            VARCHAR(100)    NULL,
        is_fo_enabled       BIT             NOT NULL CONSTRAINT df_wi_fo DEFAULT 0,
        is_active           BIT             NOT NULL CONSTRAINT df_wi_active DEFAULT 1,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_wi_created DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2       NULL,

        CONSTRAINT pk_watched_instrument PRIMARY KEY (watched_id),
        CONSTRAINT uq_watched_instrument UNIQUE (tradingsymbol, exchange)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_watched_active' AND object_id = OBJECT_ID('dbo.WatchedInstrument'))
    CREATE INDEX ix_watched_active
    ON dbo.WatchedInstrument (is_active, instrument_type, tradingsymbol);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_watched_token' AND object_id = OBJECT_ID('dbo.WatchedInstrument'))
    CREATE INDEX ix_watched_token
    ON dbo.WatchedInstrument (instrument_token);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'SignalFeatureDaily' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.SignalFeatureDaily (
        feature_id                      BIGINT          NOT NULL IDENTITY(1,1),
        signal_date                     DATE            NOT NULL,
        symbol                          VARCHAR(50)     NOT NULL,
        instrument_type                 VARCHAR(30)     NULL,

        open_915                        FLOAT           NULL,
        close_1515                      FLOAT           NULL,
        high_day                        FLOAT           NULL,
        low_day                         FLOAT           NULL,
        volume_day                      BIGINT          NULL,

        ret_1d                          FLOAT           NULL,
        ret_3d                          FLOAT           NULL,
        ret_5d                          FLOAT           NULL,
        ret_10d                         FLOAT           NULL,
        ret_20d                         FLOAT           NULL,

        sma_5                           FLOAT           NULL,
        sma_10                          FLOAT           NULL,
        sma_20                          FLOAT           NULL,
        ema_5                           FLOAT           NULL,
        ema_10                          FLOAT           NULL,
        ema_20                          FLOAT           NULL,
        close_vs_sma_5                  FLOAT           NULL,
        close_vs_sma_10                 FLOAT           NULL,
        close_vs_sma_20                 FLOAT           NULL,

        rsi_14                          FLOAT           NULL,
        macd                            FLOAT           NULL,
        macd_signal                     FLOAT           NULL,
        macd_hist                       FLOAT           NULL,
        roc_5                           FLOAT           NULL,
        roc_10                          FLOAT           NULL,

        atr_14                          FLOAT           NULL,
        atr_pct                         FLOAT           NULL,
        realized_vol_5d                 FLOAT           NULL,
        realized_vol_10d                FLOAT           NULL,
        realized_vol_20d                FLOAT           NULL,
        day_range_pct                   FLOAT           NULL,
        gap_pct                         FLOAT           NULL,
        close_position_in_range         FLOAT           NULL,

        futures_oi_change_pct           FLOAT           NULL,
        futures_volume_change_pct       FLOAT           NULL,
        pcr_oi                          FLOAT           NULL,
        pcr_volume                      FLOAT           NULL,
        atm_iv                          FLOAT           NULL,
        iv_rank_20d                     FLOAT           NULL,
        skew_put_call                   FLOAT           NULL,
        max_oi_call_strike              FLOAT           NULL,
        max_oi_put_strike               FLOAT           NULL,
        distance_from_max_call_oi_pct   FLOAT           NULL,
        distance_from_max_put_oi_pct    FLOAT           NULL,

        macro_score                     FLOAT           NULL,
        news_score                      FLOAT           NULL,
        event_risk_score                FLOAT           NULL,

        feature_version                 VARCHAR(50)     NOT NULL CONSTRAINT df_sfd_ver DEFAULT 'v1',
        source_quality_score            FLOAT           NULL,
        reason_json                     NVARCHAR(MAX)   NULL,
        strategy_features_json          NVARCHAR(MAX)   NULL,

        created_at                      DATETIME2       NOT NULL CONSTRAINT df_sfd_created DEFAULT SYSUTCDATETIME(),
        updated_at                      DATETIME2       NULL,

        CONSTRAINT pk_signal_feature_daily PRIMARY KEY (feature_id),
        CONSTRAINT uq_signal_feature_daily UNIQUE (signal_date, symbol, feature_version)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_signal_feature_symbol_date' AND object_id = OBJECT_ID('dbo.SignalFeatureDaily'))
    CREATE INDEX ix_signal_feature_symbol_date
    ON dbo.SignalFeatureDaily (symbol, signal_date);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'SignalPrediction' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.SignalPrediction (
        prediction_id       BIGINT          NOT NULL IDENTITY(1,1),
        signal_date         DATE            NOT NULL,
        trade_date          DATE            NOT NULL,
        symbol              VARCHAR(50)     NOT NULL,
        instrument_type     VARCHAR(30)     NULL,
        model_name          VARCHAR(100)    NOT NULL,
        model_version       VARCHAR(50)     NOT NULL,
        direction           VARCHAR(20)     NOT NULL,
        confidence          FLOAT           NOT NULL,
        expected_move_pct   FLOAT           NULL,
        trade_allowed       BIT             NOT NULL CONSTRAINT df_sp_trade DEFAULT 0,
        no_trade_reason     VARCHAR(200)    NULL,
        feature_id          BIGINT          NULL,
        reason_json         NVARCHAR(MAX)   NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_sp_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT pk_signal_prediction PRIMARY KEY (prediction_id),
        CONSTRAINT uq_signal_prediction UNIQUE (signal_date, trade_date, symbol, model_name, model_version)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_signal_prediction_trade_date' AND object_id = OBJECT_ID('dbo.SignalPrediction'))
    CREATE INDEX ix_signal_prediction_trade_date
    ON dbo.SignalPrediction (trade_date, trade_allowed, symbol);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_signal_prediction_symbol_date' AND object_id = OBJECT_ID('dbo.SignalPrediction'))
    CREATE INDEX ix_signal_prediction_symbol_date
    ON dbo.SignalPrediction (symbol, signal_date, trade_date);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_signal_prediction_model' AND object_id = OBJECT_ID('dbo.SignalPrediction'))
    CREATE INDEX ix_signal_prediction_model
    ON dbo.SignalPrediction (model_name, model_version, signal_date);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'SignalBacktestLabel' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.SignalBacktestLabel (
        label_id                BIGINT          NOT NULL IDENTITY(1,1),
        signal_date             DATE            NOT NULL,
        trade_date              DATE            NOT NULL,
        symbol                  VARCHAR(50)     NOT NULL,
        entry_time              DATETIME2       NULL,
        exit_time               DATETIME2       NULL,
        entry_price             FLOAT           NULL,
        exit_price              FLOAT           NULL,
        realized_return_pct     FLOAT           NULL,
        positive_threshold_pct  FLOAT           NULL,
        negative_threshold_pct  FLOAT           NULL,
        actual_label            VARCHAR(20)     NULL,
        label_version           VARCHAR(50)     NOT NULL CONSTRAINT df_sbl_ver DEFAULT 'v1',
        created_at              DATETIME2       NOT NULL CONSTRAINT df_sbl_created DEFAULT SYSUTCDATETIME(),
        updated_at              DATETIME2       NULL,

        CONSTRAINT pk_signal_backtest_label PRIMARY KEY (label_id),
        CONSTRAINT uq_signal_label UNIQUE (signal_date, trade_date, symbol, label_version)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_signal_label_symbol_date' AND object_id = OBJECT_ID('dbo.SignalBacktestLabel'))
    CREATE INDEX ix_signal_label_symbol_date
    ON dbo.SignalBacktestLabel (symbol, signal_date, trade_date);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_signal_label_actual' AND object_id = OBJECT_ID('dbo.SignalBacktestLabel'))
    CREATE INDEX ix_signal_label_actual
    ON dbo.SignalBacktestLabel (actual_label, trade_date);

-- ============================================================
-- PHASE 2: Option Selector Storage
-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'OptionCandle5m' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.OptionCandle5m (
        option_candle_id        BIGINT          NOT NULL IDENTITY(1,1),
        option_instrument_id    BIGINT          NOT NULL,
        tradingsymbol           VARCHAR(100)    NOT NULL,
        underlying              VARCHAR(50)     NOT NULL,
        candle_time             DATETIME2       NOT NULL,
        open_price              FLOAT           NULL,
        high_price              FLOAT           NULL,
        low_price               FLOAT           NULL,
        close_price             FLOAT           NULL,
        volume                  BIGINT          NULL,
        open_interest           BIGINT          NULL,
        data_purpose            VARCHAR(30)     NOT NULL,
        source                  VARCHAR(50)     NULL,
        created_at              DATETIME2       NOT NULL CONSTRAINT df_oc_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT pk_option_candle_5m PRIMARY KEY (option_candle_id),
        CONSTRAINT uq_option_candle_5m UNIQUE (option_instrument_id, candle_time)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_option_candle_symbol_time' AND object_id = OBJECT_ID('dbo.OptionCandle5m'))
    CREATE INDEX ix_option_candle_symbol_time
    ON dbo.OptionCandle5m (tradingsymbol, candle_time);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_option_candle_underlying_time' AND object_id = OBJECT_ID('dbo.OptionCandle5m'))
    CREATE INDEX ix_option_candle_underlying_time
    ON dbo.OptionCandle5m (underlying, candle_time);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_option_candle_purpose' AND object_id = OBJECT_ID('dbo.OptionCandle5m'))
    CREATE INDEX ix_option_candle_purpose
    ON dbo.OptionCandle5m (data_purpose, underlying, candle_time);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'OptionTradePlan' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.OptionTradePlan (
        trade_plan_id               BIGINT          NOT NULL IDENTITY(1,1),
        prediction_id               BIGINT          NOT NULL,
        signal_date                 DATE            NOT NULL,
        trade_date                  DATE            NOT NULL,
        underlying                  VARCHAR(50)     NOT NULL,
        direction                   VARCHAR(20)     NOT NULL,
        option_instrument_id        BIGINT          NOT NULL,
        tradingsymbol               VARCHAR(100)    NOT NULL,
        expiry                      DATE            NOT NULL,
        strike                      FLOAT           NOT NULL,
        option_type                 VARCHAR(5)      NOT NULL,
        expected_entry_time         DATETIME2       NULL,
        expected_entry_price        FLOAT           NULL,
        stop_loss_price             FLOAT           NULL,
        target_price                FLOAT           NULL,
        expected_underlying_move_pct FLOAT          NULL,
        expected_option_return_pct  FLOAT           NULL,
        expected_pnl_per_lot        FLOAT           NULL,
        max_loss_per_lot            FLOAT           NULL,
        liquidity_score             FLOAT           NULL,
        greek_score                 FLOAT           NULL,
        iv_score                    FLOAT           NULL,
        risk_reward                 FLOAT           NULL,
        total_score                 FLOAT           NULL,
        selection_reason_json       NVARCHAR(MAX)   NULL,
        status                      VARCHAR(30)     NOT NULL CONSTRAINT df_otp_status DEFAULT 'PLANNED',
        created_at                  DATETIME2       NOT NULL CONSTRAINT df_otp_created DEFAULT SYSUTCDATETIME(),
        updated_at                  DATETIME2       NULL,

        CONSTRAINT pk_option_trade_plan PRIMARY KEY (trade_plan_id)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_option_trade_plan_trade_date' AND object_id = OBJECT_ID('dbo.OptionTradePlan'))
    CREATE INDEX ix_option_trade_plan_trade_date
    ON dbo.OptionTradePlan (trade_date, status, underlying);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_option_trade_plan_prediction' AND object_id = OBJECT_ID('dbo.OptionTradePlan'))
    CREATE INDEX ix_option_trade_plan_prediction
    ON dbo.OptionTradePlan (prediction_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_option_trade_plan_symbol' AND object_id = OBJECT_ID('dbo.OptionTradePlan'))
    CREATE INDEX ix_option_trade_plan_symbol
    ON dbo.OptionTradePlan (tradingsymbol, trade_date);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'OptionPaperTradeResult' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.OptionPaperTradeResult (
        paper_trade_id              BIGINT          NOT NULL IDENTITY(1,1),
        trade_plan_id               BIGINT          NOT NULL,
        trade_date                  DATE            NOT NULL,
        underlying                  VARCHAR(50)     NOT NULL,
        tradingsymbol               VARCHAR(100)    NOT NULL,
        simulated_entry_time        DATETIME2       NULL,
        simulated_exit_time         DATETIME2       NULL,
        entry_price                 FLOAT           NULL,
        exit_price                  FLOAT           NULL,
        stop_loss_price             FLOAT           NULL,
        target_price                FLOAT           NULL,
        exit_reason                 VARCHAR(30)     NULL,
        lot_size                    INT             NULL,
        quantity                    INT             NULL,
        gross_pnl                   FLOAT           NULL,
        estimated_transaction_cost  FLOAT           NULL,
        estimated_slippage          FLOAT           NULL,
        net_pnl                     FLOAT           NULL,
        return_pct                  FLOAT           NULL,
        created_at                  DATETIME2       NOT NULL CONSTRAINT df_optr_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT pk_option_paper_trade_result PRIMARY KEY (paper_trade_id)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_paper_trade_date' AND object_id = OBJECT_ID('dbo.OptionPaperTradeResult'))
    CREATE INDEX ix_paper_trade_date
    ON dbo.OptionPaperTradeResult (trade_date, underlying);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_paper_trade_plan' AND object_id = OBJECT_ID('dbo.OptionPaperTradeResult'))
    CREATE INDEX ix_paper_trade_plan
    ON dbo.OptionPaperTradeResult (trade_plan_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_paper_trade_exit_reason' AND object_id = OBJECT_ID('dbo.OptionPaperTradeResult'))
    CREATE INDEX ix_paper_trade_exit_reason
    ON dbo.OptionPaperTradeResult (exit_reason, trade_date);

-- ============================================================
-- PHASE 3: Macro / News / Model (placeholders)
-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'MacroFactorDaily' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.MacroFactorDaily (
        macro_id                BIGINT          NOT NULL IDENTITY(1,1),
        factor_date             DATE            NOT NULL,
        india_vix               FLOAT           NULL,
        gift_nifty_return_pct   FLOAT           NULL,
        dow_return_pct          FLOAT           NULL,
        nasdaq_return_pct       FLOAT           NULL,
        sp500_return_pct        FLOAT           NULL,
        crude_return_pct        FLOAT           NULL,
        usd_inr_return_pct      FLOAT           NULL,
        bond_yield_change       FLOAT           NULL,
        event_flag              BIT             NOT NULL CONSTRAINT df_mfd_flag DEFAULT 0,
        event_type              VARCHAR(100)    NULL,
        source_json             NVARCHAR(MAX)   NULL,
        created_at              DATETIME2       NOT NULL CONSTRAINT df_mfd_created DEFAULT SYSUTCDATETIME(),
        updated_at              DATETIME2       NULL,

        CONSTRAINT pk_macro_factor_daily PRIMARY KEY (macro_id),
        CONSTRAINT uq_macro_factor_daily UNIQUE (factor_date)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_macro_factor_date' AND object_id = OBJECT_ID('dbo.MacroFactorDaily'))
    CREATE INDEX ix_macro_factor_date
    ON dbo.MacroFactorDaily (factor_date);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'NewsEvent' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.NewsEvent (
        news_id             BIGINT          NOT NULL IDENTITY(1,1),
        news_time           DATETIME2       NOT NULL,
        symbol              VARCHAR(50)     NULL,
        sector              VARCHAR(100)    NULL,
        industry            VARCHAR(100)    NULL,
        source              VARCHAR(100)    NULL,
        headline            NVARCHAR(500)   NOT NULL,
        url                 NVARCHAR(1000)  NULL,
        sentiment_score     FLOAT           NULL,
        impact_score        FLOAT           NULL,
        event_type          VARCHAR(100)    NULL,
        summary             NVARCHAR(MAX)   NULL,
        raw_json            NVARCHAR(MAX)   NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_ne_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT pk_news_event PRIMARY KEY (news_id)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_news_symbol_time' AND object_id = OBJECT_ID('dbo.NewsEvent'))
    CREATE INDEX ix_news_symbol_time ON dbo.NewsEvent (symbol, news_time);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_news_sector_time' AND object_id = OBJECT_ID('dbo.NewsEvent'))
    CREATE INDEX ix_news_sector_time ON dbo.NewsEvent (sector, news_time);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_news_time' AND object_id = OBJECT_ID('dbo.NewsEvent'))
    CREATE INDEX ix_news_time ON dbo.NewsEvent (news_time);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'CorporateEventCalendar' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.CorporateEventCalendar (
        event_id            BIGINT          NOT NULL IDENTITY(1,1),
        symbol              VARCHAR(50)     NOT NULL,
        event_date          DATE            NOT NULL,
        event_type          VARCHAR(100)    NOT NULL,
        event_description   NVARCHAR(500)   NULL,
        source              VARCHAR(100)    NULL,
        source_url          NVARCHAR(1000)  NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_cec_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT pk_corporate_event_calendar PRIMARY KEY (event_id),
        CONSTRAINT uq_corporate_event UNIQUE (symbol, event_date, event_type)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_corporate_event_symbol_date' AND object_id = OBJECT_ID('dbo.CorporateEventCalendar'))
    CREATE INDEX ix_corporate_event_symbol_date
    ON dbo.CorporateEventCalendar (symbol, event_date);

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'ModelRun' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.ModelRun (
        model_run_id        BIGINT          NOT NULL IDENTITY(1,1),
        model_name          VARCHAR(100)    NOT NULL,
        model_version       VARCHAR(50)     NOT NULL,
        run_date            DATETIME2       NOT NULL CONSTRAINT df_mr_run DEFAULT SYSUTCDATETIME(),
        train_start_date    DATE            NULL,
        train_end_date      DATE            NULL,
        test_start_date     DATE            NULL,
        test_end_date       DATE            NULL,
        config_json         NVARCHAR(MAX)   NULL,
        metrics_json        NVARCHAR(MAX)   NULL,
        notes               NVARCHAR(MAX)   NULL,

        CONSTRAINT pk_model_run PRIMARY KEY (model_run_id)
    );
END

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_model_run_name_version' AND object_id = OBJECT_ID('dbo.ModelRun'))
    CREATE INDEX ix_model_run_name_version
    ON dbo.ModelRun (model_name, model_version, run_date);

-- ============================================================
-- PHASE 4: Live Trading (placeholders)
-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'LiveOrder' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.LiveOrder (
        live_order_id       BIGINT          NOT NULL IDENTITY(1,1),
        trade_plan_id       BIGINT          NULL,
        broker              VARCHAR(50)     NOT NULL,
        broker_order_id     VARCHAR(100)    NULL,
        order_time          DATETIME2       NOT NULL CONSTRAINT df_lo_time DEFAULT SYSUTCDATETIME(),
        tradingsymbol       VARCHAR(100)    NOT NULL,
        exchange            VARCHAR(20)     NULL,
        transaction_type    VARCHAR(10)     NOT NULL,
        order_type          VARCHAR(30)     NULL,
        product_type        VARCHAR(30)     NULL,
        quantity            INT             NOT NULL,
        price               FLOAT           NULL,
        trigger_price       FLOAT           NULL,
        status              VARCHAR(50)     NULL,
        status_message      NVARCHAR(500)   NULL,
        raw_response_json   NVARCHAR(MAX)   NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_lo_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT pk_live_order PRIMARY KEY (live_order_id)
    );
END

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'LivePosition' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.LivePosition (
        live_position_id    BIGINT          NOT NULL IDENTITY(1,1),
        trade_plan_id       BIGINT          NULL,
        tradingsymbol       VARCHAR(100)    NOT NULL,
        underlying          VARCHAR(50)     NOT NULL,
        open_time           DATETIME2       NULL,
        close_time          DATETIME2       NULL,
        quantity            INT             NOT NULL,
        avg_entry_price     FLOAT           NULL,
        avg_exit_price      FLOAT           NULL,
        stop_loss_price     FLOAT           NULL,
        target_price        FLOAT           NULL,
        status              VARCHAR(30)     NOT NULL CONSTRAINT df_lp_status DEFAULT 'OPEN',
        gross_pnl           FLOAT           NULL,
        net_pnl             FLOAT           NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_lp_created DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2       NULL,

        CONSTRAINT pk_live_position PRIMARY KEY (live_position_id)
    );
END

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'ExecutionFill' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.ExecutionFill (
        execution_fill_id   BIGINT          NOT NULL IDENTITY(1,1),
        live_order_id       BIGINT          NOT NULL,
        broker_order_id     VARCHAR(100)    NULL,
        fill_time           DATETIME2       NULL,
        tradingsymbol       VARCHAR(100)    NOT NULL,
        quantity            INT             NOT NULL,
        fill_price          FLOAT           NOT NULL,
        fees                FLOAT           NULL,
        raw_json            NVARCHAR(MAX)   NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_ef_created DEFAULT SYSUTCDATETIME(),

        CONSTRAINT pk_execution_fill PRIMARY KEY (execution_fill_id)
    );
END

-- ============================================================

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'RiskLimitDaily' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.RiskLimitDaily (
        risk_limit_id       BIGINT          NOT NULL IDENTITY(1,1),
        trade_date          DATE            NOT NULL,
        max_loss_per_day    FLOAT           NOT NULL,
        max_trades_per_day  INT             NOT NULL,
        max_open_positions  INT             NOT NULL CONSTRAINT df_rld_maxpos DEFAULT 1,
        realized_pnl        FLOAT           NOT NULL CONSTRAINT df_rld_pnl DEFAULT 0,
        open_risk           FLOAT           NOT NULL CONSTRAINT df_rld_risk DEFAULT 0,
        trades_taken        INT             NOT NULL CONSTRAINT df_rld_trades DEFAULT 0,
        trading_enabled     BIT             NOT NULL CONSTRAINT df_rld_enabled DEFAULT 1,
        disable_reason      VARCHAR(300)    NULL,
        created_at          DATETIME2       NOT NULL CONSTRAINT df_rld_created DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2       NULL,

        CONSTRAINT pk_risk_limit_daily PRIMARY KEY (risk_limit_id),
        CONSTRAINT uq_risk_limit_daily UNIQUE (trade_date)
    );
END

-- ============================================================
-- VIEWS
-- ============================================================

GO

CREATE OR ALTER VIEW dbo.vw_LatestOptionSnapshot AS
WITH ranked AS (
    SELECT
        os.*,
        ROW_NUMBER() OVER (
            PARTITION BY os.option_instrument_id
            ORDER BY os.snapshot_time DESC
        ) AS rn
    FROM dbo.OptionSnapshot os
)
SELECT * FROM ranked WHERE rn = 1;

GO

CREATE OR ALTER VIEW dbo.vw_LatestOptionSnapshotWithGreeks AS
SELECT
    los.option_instrument_id,
    los.snapshot_time,
    los.underlying_price,
    los.last_price,
    los.bid_price,
    los.ask_price,
    los.volume,
    los.open_interest,
    osc.implied_volatility,
    osc.delta,
    osc.gamma,
    osc.theta,
    osc.vega
FROM dbo.vw_LatestOptionSnapshot los
LEFT JOIN dbo.OptionSnapshotCalc osc
    ON los.id = osc.option_snapshot_id;

GO

CREATE OR ALTER VIEW dbo.vw_SignalBacktestEvaluation AS
SELECT
    p.prediction_id,
    p.signal_date,
    p.trade_date,
    p.symbol,
    p.model_name,
    p.model_version,
    p.direction           AS predicted_direction,
    p.confidence,
    p.expected_move_pct,
    p.trade_allowed,
    l.actual_label,
    l.realized_return_pct,
    CASE WHEN p.direction = l.actual_label THEN 1 ELSE 0 END AS is_correct
FROM dbo.SignalPrediction p
LEFT JOIN dbo.SignalBacktestLabel l
    ON  p.signal_date = l.signal_date
    AND p.trade_date  = l.trade_date
    AND p.symbol      = l.symbol;

GO

CREATE OR ALTER VIEW dbo.vw_OptionTradePlanWithResult AS
SELECT
    tp.trade_plan_id,
    tp.prediction_id,
    tp.signal_date,
    tp.trade_date,
    tp.underlying,
    tp.direction,
    tp.tradingsymbol,
    tp.expiry,
    tp.strike,
    tp.option_type,
    tp.expected_entry_price,
    tp.stop_loss_price,
    tp.target_price,
    tp.total_score,
    tp.status,
    r.entry_price,
    r.exit_price,
    r.exit_reason,
    r.gross_pnl,
    r.estimated_transaction_cost,
    r.estimated_slippage,
    r.net_pnl,
    r.return_pct
FROM dbo.OptionTradePlan tp
LEFT JOIN dbo.OptionPaperTradeResult r ON tp.trade_plan_id = r.trade_plan_id;

GO
