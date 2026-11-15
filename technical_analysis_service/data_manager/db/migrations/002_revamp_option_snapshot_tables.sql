/*
Revamp option snapshot storage.

Destructive by design:
  - Clears dbo.OptionSnapshotCalc first.
  - Clears dbo.OptionSnapshot second.

Schema additions are idempotent and add only missing columns.
*/

IF OBJECT_ID('dbo.OptionSnapshotCalc', 'U') IS NOT NULL
BEGIN
    DELETE FROM dbo.OptionSnapshotCalc;
END

IF OBJECT_ID('dbo.OptionSnapshot', 'U') IS NOT NULL
BEGIN
    DELETE FROM dbo.OptionSnapshot;
END

GO

IF COL_LENGTH('dbo.OptionSnapshot', 'trade_date') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD trade_date DATE NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'snapshot_label') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD snapshot_label VARCHAR(20) NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'exchange_timestamp') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD exchange_timestamp DATETIME2 NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'last_trade_time') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD last_trade_time DATETIME2 NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'last_quantity') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD last_quantity INT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'average_price') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD average_price FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'buy_quantity') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD buy_quantity INT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'sell_quantity') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD sell_quantity INT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'oi_day_high') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD oi_day_high INT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'oi_day_low') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD oi_day_low INT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'bid_orders') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD bid_orders INT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'ask_orders') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD ask_orders INT NULL;

IF COL_LENGTH('dbo.OptionSnapshot', 'data_source') IS NULL
    ALTER TABLE dbo.OptionSnapshot ADD data_source VARCHAR(40) NULL;

GO

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'valuation_price') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD valuation_price FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'intrinsic_value') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD intrinsic_value FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'time_value') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD time_value FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'mid_price') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD mid_price FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'spread_width') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD spread_width FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'spread_width_pct') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD spread_width_pct FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'days_to_expiry') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD days_to_expiry FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'risk_free_rate') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD risk_free_rate FLOAT NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'calculation_status') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD calculation_status VARCHAR(30) NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'calculation_error') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc ADD calculation_error VARCHAR(500) NULL;

IF COL_LENGTH('dbo.OptionSnapshotCalc', 'created_at') IS NULL
    ALTER TABLE dbo.OptionSnapshotCalc
    ADD created_at DATETIME2 NULL
        CONSTRAINT df_option_snapshot_calc_created_at DEFAULT SYSUTCDATETIME();

GO
