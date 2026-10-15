# THE MAIN TESTED PROGRAM IS THE index_data_loader.py ---> others are untested and created by CODEX

# Index Data Table and Load Helpers

## Files

| File | Role |
|------|------|
| `scripts/Common/index_data_loader.py` | Fetches OHLC data from Yahoo Finance via `get_index_ohlc_data()` |
| `scripts/Common/index_data_db.py` | PostgreSQL table definition, engine setup, upsert helpers |
| `scripts/Common/load_daily_index_data.py` | Incremental daily loader entry point |
| `scripts/Common/index_data_loader.cron.example` | Example cron entry |

## Data shape

The DataFrame returned by `get_index_ohlc_data()` has these columns:

- `date`
- `index_name`
- `symbol`
- `open`
- `high`
- `low`
- `close`
- `volume`

Each row is one index for one trading date. Indexes covered: Nifty 50, Sensex, India VIX, S&P 500, NASDAQ, Dow Jones, Russell 2000, FTSE 100, DAX, CAC 40, Hang Seng, Nikkei 225, Shanghai Composite, KOSPI, ASX 200.

When run as a script, `index_data_loader.py` also writes `global_index_ohlc.csv` in the current working directory.

## SQL table

PostgreSQL table used by `index_data_db.py`:

```sql
CREATE TABLE IF NOT EXISTS global_index_ohlc (
    trade_date DATE NOT NULL,
    index_name VARCHAR(100) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    open NUMERIC(18, 4),
    high NUMERIC(18, 4),
    low NUMERIC(18, 4),
    close NUMERIC(18, 4),
    volume BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_global_index_ohlc PRIMARY KEY (trade_date, symbol)
);
```

## SQL upsert

Equivalent of what `upsert_index_data()` runs:

```sql
INSERT INTO global_index_ohlc (
    trade_date,
    index_name,
    symbol,
    open,
    high,
    low,
    close,
    volume
)
VALUES (
    %(trade_date)s,
    %(index_name)s,
    %(symbol)s,
    %(open)s,
    %(high)s,
    %(low)s,
    %(close)s,
    %(volume)s
)
ON CONFLICT (trade_date, symbol)
DO UPDATE SET
    index_name = EXCLUDED.index_name,
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    updated_at = CURRENT_TIMESTAMP;
```

## Python modules

### `index_data_loader.py`

- `get_index_ohlc_data(start_date, end_date)` — download and return a combined DataFrame
- `start_date` / `end_date` format: `YYYY-MM-DD`
- `end_date` is exclusive (yfinance convention)

### `index_data_db.py`

- `create_postgres_engine()` — reads `SUPABASE_CONN_STR` from `.env` via `get_settings()`
- `create_index_data_table(engine)` — creates `global_index_ohlc` if missing
- `prepare_index_data_records(index_df)` — maps loader `date` column to DB `trade_date`
- `upsert_index_data(engine, index_df)` — insert or update rows
- `get_latest_trade_date(engine)` — max `trade_date` already loaded
- `date_has_incomplete_rows(engine, trade_date)` — true if any OHLC field is NULL
- `resolve_incremental_start_date(engine, fallback_start_date)` — picks the next date to fetch:
  - empty table → `fallback_start_date`
  - latest date has incomplete rows → re-fetch from that date
  - otherwise → day after latest loaded date

## Manual load example

```python
from scripts.Common.index_data_db import (
    create_index_data_table,
    create_postgres_engine,
    upsert_index_data,
)
from scripts.Common.index_data_loader import get_index_ohlc_data

engine = create_postgres_engine()

index_df = get_index_ohlc_data(
    start_date="2026-06-19",
    end_date="2026-06-24",
)

create_index_data_table(engine)
rows_loaded = upsert_index_data(engine, index_df)
print(f"Loaded {rows_loaded} index rows")
```

## Daily incremental load

Run from repo root:

```bash
python scripts/Common/load_daily_index_data.py
```

`load_incremental_index_data()` in `load_daily_index_data.py`:

1. Connects with `create_postgres_engine()` and ensures the table exists
2. Sets `fallback_start_date = today - 7 days` (`INITIAL_LOOKBACK_DAYS`)
3. Resolves `start_date` from the DB via `resolve_incremental_start_date()`
4. Fetches through `end_date = today + 1 day` (exclusive for yfinance)
5. Upserts returned rows

The 7-day fallback covers first run and late/corrected Yahoo data. After data exists, normal runs only fetch from the day after the latest complete load.

### Recommended cron schedule (IST)

Run multiple times per day so each region has time to settle:

| Time (IST) | Markets mostly ready |
|------------|----------------------|
| 4:30 PM | India, Japan, China, Hong Kong |
| 10:30 PM | UK / Europe |
| 3:00 AM | US |

Example single entry (see `index_data_loader.cron.example`):

```cron
0 19 * * * cd /path/to/stockie_jun26 && /usr/bin/python3 scripts/Common/load_daily_index_data.py >> /var/log/index_data_loader.log 2>&1
```

Install:

```bash
crontab scripts/Common/index_data_loader.cron.example
```

Replace `/path/to/stockie_jun26` and the Python binary path before installing.

## Validation query

```sql
SELECT
    trade_date,
    COUNT(*) AS row_count
FROM global_index_ohlc
GROUP BY trade_date
ORDER BY trade_date DESC
LIMIT 10;
```
