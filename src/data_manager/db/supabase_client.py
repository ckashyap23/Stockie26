from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Any

from src.common.config import Settings
from src.common.models import OptionInstrument, WatchedInstrument


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


class SupabaseDatabaseClient:
    db_kind = "postgres"

    def __init__(self, settings: Settings) -> None:
        self._conn_str = settings.supabase_conn_str
        self._conn = None
        if not self._conn_str:
            raise RuntimeError("SUPABASE_CONN_STR is missing in .env")

    def connect(self) -> None:
        if self._conn is None:
            import psycopg2

            self._conn = psycopg2.connect(self._conn_str)
            self._conn.autocommit = False

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            raise RuntimeError("DB not connected. Call connect() first.")
        return self._conn

    def create_core_tables(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SUPABASE_CORE_SCHEMA_SQL)
        self.conn.commit()

    def upsert_watched_instruments(self, instruments: Iterable[WatchedInstrument]) -> int:
        rows = list(instruments)
        if not rows:
            return 0
        values = [
            (
                r.tradingsymbol,
                r.exchange,
                r.name,
                r.instrument_token,
                r.segment,
                r.tick_size,
                r.lot_size,
                r.instrument_type,
                r.sector,
                r.industry,
                r.is_fo_enabled,
                r.is_active,
            )
            for r in rows
        ]
        from psycopg2.extras import execute_values

        with self.conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO "WatchedInstrument"
                    (tradingsymbol, exchange, name, instrument_token, segment,
                     tick_size, lot_size, instrument_type, sector, industry,
                     is_fo_enabled, is_active)
                VALUES %s
                ON CONFLICT (tradingsymbol, exchange) DO UPDATE SET
                    name = EXCLUDED.name,
                    instrument_token = EXCLUDED.instrument_token,
                    segment = EXCLUDED.segment,
                    tick_size = EXCLUDED.tick_size,
                    lot_size = EXCLUDED.lot_size,
                    instrument_type = EXCLUDED.instrument_type,
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    is_fo_enabled = EXCLUDED.is_fo_enabled,
                    is_active = EXCLUDED.is_active,
                    updated_at = now()
                """,
                values,
            )
        self.conn.commit()
        return len(rows)

    def get_watched_instruments(
        self,
        instrument_type: str | None = None,
    ) -> list[WatchedInstrument]:
        sql = """
            SELECT watched_id, tradingsymbol, exchange, name, instrument_token,
                   segment, tick_size, lot_size, instrument_type, sector, industry,
                   is_fo_enabled, is_active
            FROM "WatchedInstrument"
            WHERE is_active = true
        """
        params: list[Any] = []
        if instrument_type:
            sql += " AND instrument_type = %s"
            params.append(instrument_type)
        sql += " ORDER BY tradingsymbol"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            WatchedInstrument(
                watched_id=r[0],
                tradingsymbol=r[1],
                exchange=r[2],
                name=r[3],
                instrument_token=int(r[4]) if r[4] is not None else None,
                segment=r[5],
                tick_size=float(r[6]) if r[6] is not None else None,
                lot_size=int(r[7]) if r[7] is not None else None,
                instrument_type=r[8],
                sector=r[9],
                industry=r[10],
                is_fo_enabled=bool(r[11]),
                is_active=bool(r[12]),
            )
            for r in rows
        ]

    def upsert_option_instruments(self, options: Iterable[OptionInstrument]) -> None:
        rows = list(options)
        if not rows:
            return
        values = [
            (
                o.fetch_date,
                o.instrument_token,
                o.underlying,
                o.exchange,
                o.tradingsymbol,
                o.name,
                o.strike,
                o.expiry,
                o.instrument_type,
                o.lot_size,
                o.tick_size,
                o.segment,
            )
            for o in rows
        ]
        from psycopg2.extras import execute_values

        with self.conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO "OptionInstrument"
                    (fetch_date, instrument_token, underlying, exchange, tradingsymbol,
                     name, strike, expiry, instrument_type, lot_size, tick_size, segment)
                VALUES %s
                ON CONFLICT (instrument_token) DO UPDATE SET
                    fetch_date = EXCLUDED.fetch_date,
                    underlying = EXCLUDED.underlying,
                    exchange = EXCLUDED.exchange,
                    tradingsymbol = EXCLUDED.tradingsymbol,
                    name = EXCLUDED.name,
                    strike = EXCLUDED.strike,
                    expiry = EXCLUDED.expiry,
                    instrument_type = EXCLUDED.instrument_type,
                    lot_size = EXCLUDED.lot_size,
                    tick_size = EXCLUDED.tick_size,
                    segment = EXCLUDED.segment
                """,
                values,
            )
        self.conn.commit()

    def upsert_underlying_snapshots(
        self,
        rows: list[tuple[str, date, datetime, float | None, float | None, float | None, float | None, int | None]],
    ) -> dict[str, int]:
        if not rows:
            return {"prepared": 0, "upserted": 0}
        from psycopg2.extras import execute_values

        with self.conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO "UnderlyingSnapshot"
                    (underlying, trade_date, loaded_at, open_price, high_price,
                     low_price, close_price, volume)
                VALUES %s
                ON CONFLICT (underlying, trade_date) DO UPDATE SET
                    loaded_at = EXCLUDED.loaded_at,
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    volume = EXCLUDED.volume
                """,
                rows,
            )
        self.conn.commit()
        return {"prepared": len(rows), "upserted": len(rows)}

    def upsert_underlying_candles_5m(
        self,
        rows: list[tuple[str, date, datetime, float, float, float, float, "int | None"]],
    ) -> dict[str, int]:
        if not rows:
            return {"prepared": 0, "updated": 0, "inserted": 0, "skipped_duplicates": 0}
        from psycopg2.extras import execute_values

        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "UnderlyingCandle5m" (
                    underlying varchar(50) NOT NULL,
                    trade_date date NOT NULL,
                    candle_time timestamp NOT NULL,
                    open_price double precision NOT NULL,
                    high_price double precision NOT NULL,
                    low_price double precision NOT NULL,
                    close_price double precision NOT NULL,
                    volume bigint,
                    CONSTRAINT pk_underlying_candle_5m PRIMARY KEY (underlying, candle_time)
                )
            """)
            execute_values(
                cur,
                """
                INSERT INTO "UnderlyingCandle5m"
                    (underlying, trade_date, candle_time, open_price, high_price,
                     low_price, close_price, volume)
                VALUES %s
                ON CONFLICT (underlying, candle_time) DO UPDATE SET
                    trade_date = EXCLUDED.trade_date,
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    volume = EXCLUDED.volume
                """,
                rows,
            )
        self.conn.commit()
        return {"prepared": len(rows), "updated": 0, "inserted": len(rows), "skipped_duplicates": 0}

    # ---------- TRADING CALENDAR ----------

    def _ensure_trading_calendar_table(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "TradingCalendar" (
                    calendar_date date NOT NULL,
                    exchange varchar(10) NOT NULL,
                    is_trading_day boolean NOT NULL DEFAULT false,
                    is_weekly_expiry boolean NOT NULL DEFAULT false,
                    is_monthly_expiry boolean NOT NULL DEFAULT false,
                    is_special_session boolean NOT NULL DEFAULT false,
                    notes text,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    CONSTRAINT pk_trading_calendar PRIMARY KEY (calendar_date, exchange)
                )
            """)

    def upsert_trading_calendar(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        self._ensure_trading_calendar_table()
        with self.conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO "TradingCalendar"
                    (calendar_date, exchange, is_trading_day, is_weekly_expiry,
                     is_monthly_expiry, is_special_session, notes)
                VALUES %s
                ON CONFLICT (calendar_date, exchange) DO UPDATE SET
                    is_trading_day     = EXCLUDED.is_trading_day,
                    is_weekly_expiry   = EXCLUDED.is_weekly_expiry,
                    is_monthly_expiry  = EXCLUDED.is_monthly_expiry,
                    is_special_session = EXCLUDED.is_special_session,
                    notes              = EXCLUDED.notes,
                    updated_at         = now()
                """,
                [
                    (
                        r["calendar_date"], r["exchange"],
                        r["is_trading_day"], r["is_weekly_expiry"],
                        r["is_monthly_expiry"], r["is_special_session"],
                        r.get("notes"),
                    )
                    for r in rows
                ],
            )
        self.conn.commit()
        return len(rows)

    def get_next_trading_day(self, signal_date: date, exchange: str = "NSE") -> date | None:
        self._ensure_trading_calendar_table()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT calendar_date
                FROM "TradingCalendar"
                WHERE exchange = %s
                  AND calendar_date > %s
                  AND is_trading_day = true
                ORDER BY calendar_date
                LIMIT 1
                """,
                (exchange, signal_date),
            )
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            return None
        return row[0].date() if isinstance(row[0], datetime) else row[0]

    def is_trading_day(self, check_date: date, exchange: str = "NSE") -> bool:
        self._ensure_trading_calendar_table()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT is_trading_day
                FROM "TradingCalendar"
                WHERE calendar_date = %s
                  AND exchange = %s
                """,
                (check_date, exchange),
            )
            row = cur.fetchone()
        self.conn.commit()
        return bool(row[0]) if row else False

    # ---------- KITE ACCESS TOKEN ----------

    def save_kite_access_token(self, access_token: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "KiteAccessToken" (
                    id SERIAL PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("SELECT COUNT(*) FROM \"KiteAccessToken\"")
            count = cur.fetchone()[0]
            if count == 0:
                cur.execute(
                    "INSERT INTO \"KiteAccessToken\" (access_token) VALUES (%s)",
                    (access_token,),
                )
            else:
                cur.execute(
                    "UPDATE \"KiteAccessToken\" SET access_token = %s, updated_at = now() "
                    "WHERE id = (SELECT id FROM \"KiteAccessToken\" ORDER BY updated_at DESC LIMIT 1)",
                    (access_token,),
                )
        self.conn.commit()

    def bulk_insert_option_snapshots(
        self,
        rows: list[dict],
        batch_size: int = 500,
    ) -> int:
        """
        Upsert option snapshot rows into Supabase "OptionSnapshot".

        Each dict must have: option_instrument_id, snapshot_time, trade_date,
        snapshot_label. All other fields are optional.
        Upsert key: (option_instrument_id, trade_date, snapshot_label).
        """
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        with self.conn.cursor() as cur:
            for batch_start in range(0, len(rows), batch_size):
                batch = rows[batch_start : batch_start + batch_size]
                values = [
                    (
                        r["option_instrument_id"],
                        r["snapshot_time"],
                        r.get("underlying_price"),
                        r.get("last_price"),
                        r.get("bid_price"),
                        r.get("bid_qty"),
                        r.get("ask_price"),
                        r.get("ask_qty"),
                        r.get("volume"),
                        r.get("open_interest"),
                        r["trade_date"],
                        r["snapshot_label"],
                        r.get("exchange_timestamp"),
                        r.get("last_trade_time"),
                        r.get("last_quantity"),
                        r.get("average_price"),
                        r.get("buy_quantity"),
                        r.get("sell_quantity"),
                        r.get("oi_day_high"),
                        r.get("oi_day_low"),
                        r.get("bid_orders"),
                        r.get("ask_orders"),
                        r.get("data_source"),
                    )
                    for r in batch
                ]
                execute_values(
                    cur,
                    """
                    INSERT INTO "OptionSnapshot" (
                        option_instrument_id, snapshot_time,
                        underlying_price, last_price,
                        bid_price, bid_qty, ask_price, ask_qty,
                        volume, open_interest,
                        trade_date, snapshot_label,
                        exchange_timestamp, last_trade_time, last_quantity,
                        average_price, buy_quantity, sell_quantity,
                        oi_day_high, oi_day_low, bid_orders, ask_orders,
                        data_source
                    )
                    VALUES %s
                    ON CONFLICT (option_instrument_id, trade_date, snapshot_label)
                    DO UPDATE SET
                        snapshot_time    = EXCLUDED.snapshot_time,
                        underlying_price = EXCLUDED.underlying_price,
                        last_price       = EXCLUDED.last_price,
                        bid_price        = EXCLUDED.bid_price,
                        bid_qty          = EXCLUDED.bid_qty,
                        ask_price        = EXCLUDED.ask_price,
                        ask_qty          = EXCLUDED.ask_qty,
                        volume           = EXCLUDED.volume,
                        open_interest    = EXCLUDED.open_interest,
                        exchange_timestamp = EXCLUDED.exchange_timestamp,
                        last_trade_time  = EXCLUDED.last_trade_time,
                        last_quantity    = EXCLUDED.last_quantity,
                        average_price    = EXCLUDED.average_price,
                        buy_quantity     = EXCLUDED.buy_quantity,
                        sell_quantity    = EXCLUDED.sell_quantity,
                        oi_day_high      = EXCLUDED.oi_day_high,
                        oi_day_low       = EXCLUDED.oi_day_low,
                        bid_orders       = EXCLUDED.bid_orders,
                        ask_orders       = EXCLUDED.ask_orders,
                        data_source      = EXCLUDED.data_source
                    """,
                    values,
                )
        self.conn.commit()
        return len(rows)

    def upsert_signal_features(self, rows: list[dict]) -> int:
        """
        Persist feature rows to "SignalFeatureDaily".

        Each dict must have: signal_date, symbol. All feature columns are optional.
        Upsert key: (signal_date, symbol, feature_version).
        """
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        cols = [
            "signal_date", "symbol", "feature_version",
            "close_1515", "open_915", "high_day", "low_day", "volume_day",
            "ma10", "ma20", "ma50", "ma90",
            "rsi14", "rsi5", "atr7", "atr7_sma", "atr14", "atr14_sma",
            "bb_upper", "bb_middle", "bb_lower", "bb_width",
            "ret_2d", "ret_3d", "ret_5d", "ret_10d", "ret_20d", "ret_60d",
            "volatility_10d", "volatility_20d", "volume_10d", "volume_20d", "volume_hybrid",
            "trend_efficiency_5d", "trend_efficiency_10d",
            "trend_efficiency_20d", "trend_efficiency_60d",
            "relative_strength_vs_sector",
            "ma5d_slope", "ma10d_slope", "ma20_slope", "ma50_slope", "ma_slope_combo",
            "recent_high_5d", "recent_low_5d",
            "recent_high_10d", "recent_low_10d",
            "support_level_10d", "resistance_level_10d",
            "support_distance_10d", "resistance_distance_10d",
            "recent_high_20d", "recent_low_20d",
            "range_position_5d", "range_position_10d", "range_position_20d",
            "support_bounce_count_10d", "resistance_rejection_count_10d",
            "support_broken_10d", "resistance_broken_10d",
            "near_validated_support_10d", "near_validated_resistance_10d",
            "room_to_validated_resistance_10d",
            "regime",
        ]
        update_cols = [c for c in cols if c not in ("signal_date", "symbol", "feature_version")]
        set_clause = ",\n                        ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

        with self.conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE "SignalFeatureDaily"
                    ADD COLUMN IF NOT EXISTS ret_10d double precision,
                    ADD COLUMN IF NOT EXISTS rsi5 double precision,
                    ADD COLUMN IF NOT EXISTS ret_2d double precision,
                    ADD COLUMN IF NOT EXISTS ret_3d double precision,
                    ADD COLUMN IF NOT EXISTS volatility_10d double precision,
                    ADD COLUMN IF NOT EXISTS volume_10d double precision,
                    ADD COLUMN IF NOT EXISTS volume_20d double precision,
                    ADD COLUMN IF NOT EXISTS volume_hybrid double precision,
                    ADD COLUMN IF NOT EXISTS trend_efficiency_5d double precision,
                    ADD COLUMN IF NOT EXISTS trend_efficiency_10d double precision,
                    ADD COLUMN IF NOT EXISTS trend_efficiency_20d double precision,
                    ADD COLUMN IF NOT EXISTS ma5d_slope double precision,
                    ADD COLUMN IF NOT EXISTS ma10d_slope double precision,
                    ADD COLUMN IF NOT EXISTS ma_slope_combo double precision,
                    ADD COLUMN IF NOT EXISTS recent_high_5d double precision,
                    ADD COLUMN IF NOT EXISTS recent_low_5d double precision,
                    ADD COLUMN IF NOT EXISTS recent_high_10d double precision,
                    ADD COLUMN IF NOT EXISTS recent_low_10d double precision,
                    ADD COLUMN IF NOT EXISTS support_level_10d double precision,
                    ADD COLUMN IF NOT EXISTS resistance_level_10d double precision,
                    ADD COLUMN IF NOT EXISTS support_distance_10d double precision,
                    ADD COLUMN IF NOT EXISTS resistance_distance_10d double precision,
                    ADD COLUMN IF NOT EXISTS support_bounce_count_10d integer,
                    ADD COLUMN IF NOT EXISTS resistance_rejection_count_10d integer,
                    ADD COLUMN IF NOT EXISTS support_broken_10d boolean,
                    ADD COLUMN IF NOT EXISTS resistance_broken_10d boolean,
                    ADD COLUMN IF NOT EXISTS near_validated_support_10d boolean,
                    ADD COLUMN IF NOT EXISTS near_validated_resistance_10d boolean,
                    ADD COLUMN IF NOT EXISTS room_to_validated_resistance_10d double precision,
                    ADD COLUMN IF NOT EXISTS range_position_5d double precision,
                    ADD COLUMN IF NOT EXISTS range_position_10d double precision,
                    ADD COLUMN IF NOT EXISTS atr14_sma double precision,
                    ADD COLUMN IF NOT EXISTS atr7 double precision,
                    ADD COLUMN IF NOT EXISTS atr7_sma double precision,
                    DROP COLUMN IF EXISTS volume_ratio,
                    DROP COLUMN IF EXISTS ma20_50_crossovers_20d
            """)
            values = [tuple(r.get(c, "v1" if c == "feature_version" else None) for c in cols) for r in rows]
            execute_values(
                cur,
                f"""
                INSERT INTO "SignalFeatureDaily" ({", ".join(cols)}, updated_at)
                VALUES %s
                ON CONFLICT ON CONSTRAINT uq_signal_feature_daily DO UPDATE SET
                    {set_clause},
                    updated_at = now()
                """,
                [v + (None,) for v in values],
            )
        self.conn.commit()
        return len(rows)

    def upsert_nifty_predictions(self, rows: list[dict]) -> int:
        """
        Persist daily final-prediction rows to "NiftyPrediction".

        Each dict mirrors the production prediction CSV. Required: signal_date.
        Upsert key: (symbol, signal_date, model_version). The table is created on
        first use, so a missing migration does not break the daily job.
        """
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        cols = [
            "symbol", "signal_date", "model_version", "next_trade_date",
            "open_915", "high_day", "low_day", "close_1515", "volume_day",
            "vix_close", "vix_chg_1d", "vix_chg_pct", "regime",
            "next_open", "next_high", "next_low", "next_close", "next_return_pct",
            "final_prediction", "watch_signal", "prior_watch_signal", "prior_watch_age",
            "promoted_prediction", "effective_prediction", "promotion_reason",
            "direction", "volatility_regime", "primary_strategy",
            "primary_strategy_family", "primary_strategy_type",
            "strategy_precision", "signal_style", "strength_score", "strength_label",
            "confidence_level", "actual_trade_label",
            "bull_score", "bear_score", "signal_quality", "actual_quality_label",
            "quality_horizon_days",
            "global_risk_off", "global_gate_reason",
            "global_us_return_mean", "global_europe_return_mean", "global_asia_return_mean",
            "watch_family", "watch_variant", "watch_strategy_type",
            "prior_watch_family", "prior_watch_variant", "prior_watch_strategy_type",
            "confirming_family", "confirming_variant", "confirming_strategy_type",
            "family_confirmation_match",
            "promotion_block_reason",
        ]
        key_cols = ("symbol", "signal_date", "model_version")
        update_cols = [c for c in cols if c not in key_cols]
        set_clause = ",\n                        ".join(
            f"{c} = EXCLUDED.{c}" for c in update_cols
        )

        with self.conn.cursor() as cur:
            # Rename legacy column if the table was created before this migration.
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'NiftyPrediction' AND column_name = 'trade_date'
                    ) THEN
                        ALTER TABLE "NiftyPrediction" RENAME COLUMN trade_date TO signal_date;
                    END IF;
                END$$;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "NiftyPrediction" (
                    symbol             varchar(50)  NOT NULL DEFAULT 'NIFTY',
                    signal_date        date         NOT NULL,
                    model_version      varchar(50)  NOT NULL DEFAULT 'cascade_v1',
                    next_trade_date    date,
                    open_915           double precision,
                    high_day           double precision,
                    low_day            double precision,
                    close_1515         double precision,
                    volume_day         double precision,
                    vix_close          double precision,
                    vix_chg_1d         double precision,
                    vix_chg_pct        double precision,
                    regime             varchar(20),
                    next_open          double precision,
                    next_high          double precision,
                    next_low           double precision,
                    next_close         double precision,
                    next_return_pct    double precision,
                    final_prediction   varchar(20),
                    watch_signal       varchar(20),
                    prior_watch_signal varchar(20),
                    prior_watch_age    smallint,
                    promoted_prediction varchar(20),
                    effective_prediction varchar(20),
                    promotion_reason   varchar(500),
                    direction          varchar(20),
                    volatility_regime  varchar(20),
                    primary_strategy   varchar(120),
                    primary_strategy_family varchar(80),
                    primary_strategy_type varchar(40),
                    strategy_precision double precision,
                    signal_style       varchar(50),
                    strength_score     double precision,
                    strength_label     varchar(20),
                    confidence_level   double precision,
                    actual_trade_label varchar(20),
                    bull_score         double precision,
                    bear_score         double precision,
                    signal_quality     double precision,
                    actual_quality_label varchar(20),
                    quality_horizon_days integer,
                    watch_family varchar(80),
                    watch_variant varchar(120),
                    watch_strategy_type varchar(40),
                    prior_watch_family varchar(80),
                    prior_watch_variant varchar(120),
                    prior_watch_strategy_type varchar(40),
                    confirming_family varchar(80),
                    confirming_variant varchar(120),
                    confirming_strategy_type varchar(40),
                    family_confirmation_match boolean,
                    promotion_block_reason varchar(120),
                    created_at         timestamptz NOT NULL DEFAULT now(),
                    updated_at         timestamptz NOT NULL DEFAULT now(),
                    CONSTRAINT pk_nifty_prediction PRIMARY KEY (symbol, signal_date, model_version)
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_nifty_prediction_date
                    ON "NiftyPrediction" (signal_date);
            """)
            for ddl in (
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS direction varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS volatility_regime varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS primary_strategy varchar(120)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS primary_strategy_family varchar(80)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS primary_strategy_type varchar(40)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS strategy_precision double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS signal_style varchar(50)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS strength_score double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS strength_label varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS confidence_level double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS watch_signal varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS prior_watch_signal varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS prior_watch_age smallint',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS promoted_prediction varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS effective_prediction varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS promotion_reason varchar(500)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS global_risk_off boolean',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS global_gate_reason varchar(50)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS global_us_return_mean double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS global_europe_return_mean double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS global_asia_return_mean double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS bull_score double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS bear_score double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS signal_quality double precision',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS actual_quality_label varchar(20)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS quality_horizon_days integer',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS watch_family varchar(80)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS watch_variant varchar(120)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS watch_strategy_type varchar(40)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS prior_watch_family varchar(80)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS prior_watch_variant varchar(120)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS prior_watch_strategy_type varchar(40)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS confirming_family varchar(80)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS confirming_variant varchar(120)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS confirming_strategy_type varchar(40)',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS family_confirmation_match boolean',
                'ALTER TABLE "NiftyPrediction" ADD COLUMN IF NOT EXISTS promotion_block_reason varchar(120)',
            ):
                cur.execute(ddl)
            values = [
                tuple(
                    r.get(c, "NIFTY" if c == "symbol"
                          else "cascade_v1" if c == "model_version" else None)
                    for c in cols
                )
                for r in rows
            ]
            execute_values(
                cur,
                f"""
                INSERT INTO "NiftyPrediction" ({", ".join(cols)})
                VALUES %s
                ON CONFLICT ON CONSTRAINT pk_nifty_prediction DO UPDATE SET
                    {set_clause},
                    updated_at = now()
                """,
                values,
            )
        self.conn.commit()
        return len(rows)

    def upsert_nifty_option_selections(self, rows: list[dict]) -> int:
        """Persist daily option-selection rows to "NiftyOptionSelection"."""
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        cols = [
            "symbol", "trade_date", "model_version", "next_trade_date",
            "final_prediction", "prediction_direction", "volatility_regime",
            "primary_strategy", "strategy_precision", "signal_style",
            "strength_score", "strength_label", "confidence_level",
            "spot_price", "as_of_time", "selected_strategy", "option_bias_selected",
            "no_trade_reason", "evaluated_candidate_count", "strategy_direction",
            "entry_debit_or_credit", "max_profit", "max_loss", "breakeven",
            "reward_risk", "selection_score", "selection_confidence",
            "total_delta", "total_gamma", "total_theta", "total_vega",
            "legs_summary", "primary_buy_token", "primary_buy_symbol",
            "primary_buy_strike", "primary_buy_expiry", "primary_buy_option_type",
            "primary_buy_entry_price", "primary_buy_iv", "primary_buy_delta",
            "target_1_pct", "target_1_price", "target_2_pct", "target_2_price",
            "stop_loss_enabled", "stop_loss_pct", "stop_loss_price",
        ]
        key_cols = ("symbol", "trade_date", "model_version")
        update_cols = [c for c in cols if c not in key_cols]
        set_clause = ",\n                        ".join(
            f"{c} = EXCLUDED.{c}" for c in update_cols
        )

        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "NiftyOptionSelection" (
                    symbol                    varchar(50) NOT NULL DEFAULT 'NIFTY',
                    trade_date                date        NOT NULL,
                    model_version             varchar(50) NOT NULL DEFAULT 'cascade_v1',
                    next_trade_date           date,
                    final_prediction          varchar(20),
                    prediction_direction      varchar(20),
                    volatility_regime         varchar(20),
                    primary_strategy          varchar(120),
                    strategy_precision        double precision,
                    signal_style              varchar(50),
                    strength_score            double precision,
                    strength_label            varchar(20),
                    confidence_level          double precision,
                    spot_price                double precision,
                    as_of_time                timestamp,
                    selected_strategy         varchar(50),
                    option_bias_selected      varchar(50),
                    no_trade_reason           text,
                    evaluated_candidate_count integer,
                    strategy_direction        varchar(20),
                    entry_debit_or_credit     double precision,
                    max_profit                double precision,
                    max_loss                  double precision,
                    breakeven                 double precision,
                    reward_risk               double precision,
                    selection_score           double precision,
                    selection_confidence      varchar(20),
                    total_delta               double precision,
                    total_gamma               double precision,
                    total_theta               double precision,
                    total_vega                double precision,
                    legs_summary              text,
                    primary_buy_token         bigint,
                    primary_buy_symbol        varchar(120),
                    primary_buy_strike        double precision,
                    primary_buy_expiry        date,
                    primary_buy_option_type   varchar(10),
                    primary_buy_entry_price   double precision,
                    primary_buy_iv            double precision,
                    primary_buy_delta         double precision,
                    target_1_pct              double precision,
                    target_1_price            double precision,
                    target_2_pct              double precision,
                    target_2_price            double precision,
                    stop_loss_enabled         boolean NOT NULL DEFAULT false,
                    stop_loss_pct             double precision,
                    stop_loss_price           double precision,
                    created_at                timestamptz NOT NULL DEFAULT now(),
                    updated_at                timestamptz NOT NULL DEFAULT now(),
                    CONSTRAINT pk_nifty_option_selection PRIMARY KEY (symbol, trade_date, model_version)
                );
            """)
            for ddl in (
                'ALTER TABLE "NiftyOptionSelection" ADD COLUMN IF NOT EXISTS target_1_pct double precision',
                'ALTER TABLE "NiftyOptionSelection" ADD COLUMN IF NOT EXISTS target_1_price double precision',
                'ALTER TABLE "NiftyOptionSelection" ADD COLUMN IF NOT EXISTS target_2_pct double precision',
                'ALTER TABLE "NiftyOptionSelection" ADD COLUMN IF NOT EXISTS target_2_price double precision',
                'ALTER TABLE "NiftyOptionSelection" ADD COLUMN IF NOT EXISTS stop_loss_enabled boolean NOT NULL DEFAULT false',
                'ALTER TABLE "NiftyOptionSelection" ADD COLUMN IF NOT EXISTS stop_loss_pct double precision',
                'ALTER TABLE "NiftyOptionSelection" ADD COLUMN IF NOT EXISTS stop_loss_price double precision',
            ):
                cur.execute(ddl)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_nifty_option_selection_date
                    ON "NiftyOptionSelection" (trade_date);
            """)
            values = [
                tuple(
                    r.get(c, "NIFTY" if c == "symbol"
                          else "cascade_v1" if c == "model_version" else None)
                    for c in cols
                )
                for r in rows
            ]
            execute_values(
                cur,
                f"""
                INSERT INTO "NiftyOptionSelection" ({", ".join(cols)})
                VALUES %s
                ON CONFLICT ON CONSTRAINT pk_nifty_option_selection DO UPDATE SET
                    {set_clause},
                    updated_at = now()
                """,
                values,
            )
        self.conn.commit()
        return len(rows)

    # ---------- PAPER TRADING ----------

    def ensure_paper_trade_tables(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("""
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
                    target_1_pct               double precision,
                    target_1_price             double precision,
                    target_2_pct               double precision,
                    target_2_price             double precision,
                    stop_loss_price            double precision,
                    status                     varchar(30) NOT NULL DEFAULT 'PLANNED',
                    source_selection_trade_date date NOT NULL,
                    source_final_prediction     varchar(20),
                    promoted_prediction         varchar(20),
                    signal_day_close_1515       double precision,
                    entry_action                varchar(40),
                    opening_gap_pct             double precision,
                    call_reclaim_level          double precision,
                    error_message              text,
                    created_at                 timestamptz NOT NULL DEFAULT now(),
                    updated_at                 timestamptz NOT NULL DEFAULT now(),
                    CONSTRAINT uq_paper_execution_signal UNIQUE
                        (symbol, model_version, signal_trade_date, paper_trade_date, paper_platform)
                )
            """)
            cur.execute(
                'ALTER TABLE "PaperExecutionSignal" ADD COLUMN IF NOT EXISTS target_1_pct double precision'
            )
            cur.execute(
                'ALTER TABLE "PaperExecutionSignal" ADD COLUMN IF NOT EXISTS target_2_pct double precision'
            )
            cur.execute(
                'ALTER TABLE "PaperExecutionSignal" ADD COLUMN IF NOT EXISTS stop_loss_pct double precision'
            )
            for ddl in (
                'ADD COLUMN IF NOT EXISTS source_final_prediction varchar(20)',
                'ADD COLUMN IF NOT EXISTS promoted_prediction varchar(20)',
                'ADD COLUMN IF NOT EXISTS signal_day_close_1515 double precision',
                'ADD COLUMN IF NOT EXISTS entry_action varchar(40)',
                'ADD COLUMN IF NOT EXISTS opening_gap_pct double precision',
                'ADD COLUMN IF NOT EXISTS call_reclaim_level double precision',
                'ADD COLUMN IF NOT EXISTS sl_divider double precision',
                'ADD COLUMN IF NOT EXISTS completed_targets integer NOT NULL DEFAULT 0',
            ):
                cur.execute(f'ALTER TABLE "PaperExecutionSignal" {ddl}')
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_paper_execution_signal_due
                    ON "PaperExecutionSignal" (paper_trade_date, status, symbol)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "PaperOrder" (
                    id                         bigserial PRIMARY KEY,
                    paper_execution_signal_id  bigint NOT NULL REFERENCES "PaperExecutionSignal"(id),
                    paper_platform             varchar(30) NOT NULL DEFAULT 'STOCKIE',
                    order_role                 varchar(20) NOT NULL,
                    side                       varchar(10) NOT NULL,
                    exchange                   varchar(10) NOT NULL DEFAULT 'NFO',
                    variety                    varchar(20) NOT NULL DEFAULT 'regular',
                    product                    varchar(20) NOT NULL DEFAULT 'NRML',
                    order_type                 varchar(20) NOT NULL DEFAULT 'MARKET',
                    quantity                   integer NOT NULL,
                    requested_price            double precision,
                    filled_price               double precision,
                    market_bid_price           double precision,
                    market_bid_quantity        integer,
                    market_ask_price           double precision,
                    market_ask_quantity        integer,
                    bid_ask_spread             double precision,
                    market_quote_time          timestamptz,
                    transaction_tax            double precision,
                    transaction_tax_type       varchar(20),
                    exchange_turnover_charge   double precision,
                    sebi_turnover_charge       double precision,
                    brokerage                  double precision,
                    stamp_duty                 double precision,
                    gst_igst                   double precision,
                    gst_cgst                   double precision,
                    gst_sgst                   double precision,
                    gst_total                  double precision,
                    total_charges              double precision,
                    charges_status             varchar(30),
                    charges_calculated_at      timestamptz,
                    charges_payload_json       jsonb,
                    charges_error              text,
                    status                     varchar(30) NOT NULL,
                    payload_json               jsonb,
                    error_message              text,
                    created_at                 timestamptz NOT NULL DEFAULT now(),
                    updated_at                 timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                ALTER TABLE "PaperOrder"
                    ADD COLUMN IF NOT EXISTS exchange varchar(10) NOT NULL DEFAULT 'NFO',
                    ADD COLUMN IF NOT EXISTS variety varchar(20) NOT NULL DEFAULT 'regular',
                    ADD COLUMN IF NOT EXISTS product varchar(20) NOT NULL DEFAULT 'NRML',
                    ADD COLUMN IF NOT EXISTS market_bid_price double precision,
                    ADD COLUMN IF NOT EXISTS market_bid_quantity integer,
                    ADD COLUMN IF NOT EXISTS market_ask_price double precision,
                    ADD COLUMN IF NOT EXISTS market_ask_quantity integer,
                    ADD COLUMN IF NOT EXISTS bid_ask_spread double precision,
                    ADD COLUMN IF NOT EXISTS market_quote_time timestamptz,
                    ADD COLUMN IF NOT EXISTS transaction_tax double precision,
                    ADD COLUMN IF NOT EXISTS transaction_tax_type varchar(20),
                    ADD COLUMN IF NOT EXISTS exchange_turnover_charge double precision,
                    ADD COLUMN IF NOT EXISTS sebi_turnover_charge double precision,
                    ADD COLUMN IF NOT EXISTS brokerage double precision,
                    ADD COLUMN IF NOT EXISTS stamp_duty double precision,
                    ADD COLUMN IF NOT EXISTS gst_igst double precision,
                    ADD COLUMN IF NOT EXISTS gst_cgst double precision,
                    ADD COLUMN IF NOT EXISTS gst_sgst double precision,
                    ADD COLUMN IF NOT EXISTS gst_total double precision,
                    ADD COLUMN IF NOT EXISTS total_charges double precision,
                    ADD COLUMN IF NOT EXISTS charges_status varchar(30),
                    ADD COLUMN IF NOT EXISTS charges_calculated_at timestamptz,
                    ADD COLUMN IF NOT EXISTS charges_payload_json jsonb,
                    ADD COLUMN IF NOT EXISTS charges_error text
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_paper_order_signal
                    ON "PaperOrder" (paper_execution_signal_id, order_role, status)
            """)
            cur.execute("""
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
                    entry_charges              double precision,
                    exit_charges               double precision,
                    total_charges              double precision,
                    net_pnl_per_lot             double precision,
                    net_return_pct              double precision,
                    status                     varchar(30) NOT NULL DEFAULT 'OPEN',
                    source                     varchar(30) NOT NULL DEFAULT 'STOCKIE',
                    created_at                 timestamptz NOT NULL DEFAULT now(),
                    updated_at                 timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                ALTER TABLE "PaperTradeResult"
                    ADD COLUMN IF NOT EXISTS entry_charges double precision,
                    ADD COLUMN IF NOT EXISTS exit_charges double precision,
                    ADD COLUMN IF NOT EXISTS total_charges double precision,
                    ADD COLUMN IF NOT EXISTS net_pnl_per_lot double precision,
                    ADD COLUMN IF NOT EXISTS net_return_pct double precision
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_paper_trade_result_status
                    ON "PaperTradeResult" (status, current_quote_time)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "PaperTradeEvent" (
                    id                         bigserial PRIMARY KEY,
                    paper_execution_signal_id  bigint NOT NULL REFERENCES "PaperExecutionSignal"(id),
                    event_time                 timestamptz NOT NULL DEFAULT now(),
                    event_type                 varchar(50) NOT NULL,
                    price                      double precision,
                    message                    text,
                    payload_json               jsonb
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_paper_trade_event_signal
                    ON "PaperTradeEvent" (paper_execution_signal_id, event_time)
            """)
        self.conn.commit()

    def prepare_paper_execution_signals(
        self,
        trade_date: date,
        symbol: str = "NIFTY",
        model_version: str = "cascade_v1",
        paper_platform: str = "STOCKIE",
    ) -> int:
        from src.common.config import get_target_pct_for_regime, get_sl_pct_for_regime

        self.ensure_paper_trade_tables()

        # Fetch regime per candidate row so we can apply env-configured pcts per row.
        from psycopg2.extras import RealDictCursor
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT o.trade_date, o.next_trade_date, o.symbol, o.model_version,
                       o.prediction_direction, o.selected_strategy, o.primary_strategy,
                       o.primary_buy_symbol, o.primary_buy_token, o.primary_buy_option_type,
                       o.primary_buy_entry_price, o.volatility_regime,
                       COALESCE(oi.lot_size, 1) AS quantity, oi.lot_size,
                       p.final_prediction AS source_final_prediction,
                       p.promoted_prediction, p.close_1515 AS signal_day_close_1515
                FROM "NiftyOptionSelection" o
                JOIN "NiftyPrediction" p
                  ON p.symbol = o.symbol
                 AND p.signal_date = o.trade_date
                 AND p.model_version = o.model_version
                LEFT JOIN "OptionInstrument" oi ON oi.instrument_token = o.primary_buy_token
                WHERE UPPER(o.symbol) = %s
                  AND o.model_version = %s
                  AND o.next_trade_date = %s
                  AND p.effective_prediction IN ('CALL', 'PUT')
                  AND o.primary_buy_token IS NOT NULL
                  AND o.primary_buy_symbol IS NOT NULL
                  AND o.primary_buy_entry_price IS NOT NULL
                """,
                (symbol.upper(), model_version, trade_date),
            )
            candidates = cur.fetchall()

        inserted = 0
        with self.conn.cursor() as cur:
            for row in candidates:
                regime = (row.get("volatility_regime") or "calm")
                target_pct = get_target_pct_for_regime(regime)
                sl_pct = get_sl_pct_for_regime(regime)
                from src.common.config import get_sl_divider_for_regime
                sl_divider = get_sl_divider_for_regime(regime)
                cur.execute(
                    """
                    INSERT INTO "PaperExecutionSignal" (
                        symbol, model_version, signal_trade_date, paper_trade_date,
                        paper_platform, direction, selected_strategy,
                        prediction_strategy, option_symbol, option_token, option_type,
                        quantity, lot_size, planned_entry_price,
                        target_1_pct, target_1_price,
                        target_2_pct, target_2_price,
                        stop_loss_pct, stop_loss_price,
                        source_selection_trade_date, source_final_prediction,
                        promoted_prediction, signal_day_close_1515, entry_action
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, NULL, %s, NULL, %s, NULL, %s,
                        %s, %s, %s, 'PENDING'
                    )
                    ON CONFLICT ON CONSTRAINT uq_paper_execution_signal DO UPDATE SET
                        target_1_pct = EXCLUDED.target_1_pct,
                        target_1_price = NULL,
                        target_2_pct = EXCLUDED.target_2_pct,
                        target_2_price = NULL,
                        stop_loss_pct = EXCLUDED.stop_loss_pct,
                        stop_loss_price = NULL,
                        source_final_prediction = EXCLUDED.source_final_prediction,
                        promoted_prediction = EXCLUDED.promoted_prediction,
                        signal_day_close_1515 = EXCLUDED.signal_day_close_1515,
                        entry_action = 'PENDING',
                        updated_at = now()
                    WHERE "PaperExecutionSignal".status = 'PLANNED'
                    """,
                    (
                        row["symbol"], row["model_version"],
                        row["trade_date"], row["next_trade_date"],
                        paper_platform,
                        row["prediction_direction"], row["selected_strategy"],
                        row["primary_strategy"], row["primary_buy_symbol"],
                        row["primary_buy_token"], row["primary_buy_option_type"],
                        row["quantity"], row["lot_size"],
                        row["primary_buy_entry_price"],
                        target_pct, None, sl_pct,
                        row["trade_date"],
                        row["source_final_prediction"], row["promoted_prediction"],
                        row["signal_day_close_1515"],
                    ),
                )
                inserted += cur.rowcount
                cur.execute(
                    '''UPDATE "PaperExecutionSignal"
                       SET sl_divider=%s, completed_targets=0
                       WHERE symbol=%s AND model_version=%s
                         AND signal_trade_date=%s AND paper_trade_date=%s
                         AND paper_platform=%s AND status='PLANNED' ''',
                    (sl_divider, row["symbol"], row["model_version"], row["trade_date"],
                     row["next_trade_date"], paper_platform),
                )
        self.conn.commit()
        return inserted

    def set_paper_entry_action(
        self,
        signal_id: int,
        entry_action: str,
        opening_gap_pct: float | None = None,
        call_reclaim_level: float | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                '''
                UPDATE "PaperExecutionSignal"
                SET entry_action = %s, opening_gap_pct = %s,
                    call_reclaim_level = %s, updated_at = now()
                WHERE id = %s
                ''',
                (entry_action, opening_gap_pct, call_reclaim_level, signal_id),
            )
        self.conn.commit()

    def list_paper_execution_signals(
        self,
        trade_date: date | None = None,
        statuses: tuple[str, ...] = ("PLANNED",),
        symbol: str = "NIFTY",
        model_version: str = "cascade_v1",
    ) -> list[dict]:
        self.ensure_paper_trade_tables()
        from psycopg2.extras import RealDictCursor

        params: list[Any] = [symbol.upper(), model_version, list(statuses)]
        date_filter = ""
        if trade_date is not None:
            date_filter = "AND paper_trade_date = %s"
            params.append(trade_date)
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT *
                FROM "PaperExecutionSignal"
                WHERE UPPER(symbol) = %s
                  AND model_version = %s
                  AND status = ANY(%s)
                  {date_filter}
                ORDER BY paper_trade_date, signal_trade_date, id
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def list_open_paper_trades(
        self,
        trade_date: date | None = None,
        symbol: str = "NIFTY",
        model_version: str = "cascade_v1",
    ) -> list[dict]:
        self.ensure_paper_trade_tables()
        from psycopg2.extras import RealDictCursor

        params: list[Any] = [symbol.upper(), model_version]
        date_filter = ""
        if trade_date is not None:
            date_filter = "AND s.paper_trade_date = %s"
            params.append(trade_date)
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT s.*, r.id AS paper_trade_result_id, r.entry_price,
                       r.entry_time, r.current_price, r.current_quote_time,
                       r.pnl_points, r.pnl_per_lot, r.return_pct
                FROM "PaperExecutionSignal" s
                JOIN "PaperTradeResult" r
                  ON r.paper_execution_signal_id = s.id
                WHERE UPPER(s.symbol) = %s
                  AND s.model_version = %s
                  AND s.status = 'OPEN'
                  AND r.status = 'OPEN'
                  {date_filter}
                ORDER BY s.paper_trade_date, s.id
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def list_paper_trade_results(
        self,
        trade_date: date,
        statuses: tuple[str, ...] = ("OPEN", "CLOSED"),
        symbol: str = "NIFTY",
        model_version: str = "cascade_v1",
    ) -> list[dict]:
        self.ensure_paper_trade_tables()
        from psycopg2.extras import RealDictCursor

        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.symbol, s.model_version, s.signal_trade_date,
                       s.paper_trade_date, s.direction, s.selected_strategy,
                       s.prediction_strategy, s.option_symbol, s.option_token,
                       s.option_type, s.quantity, s.lot_size,
                       s.planned_entry_price, s.target_1_pct, s.target_1_price,
                       s.stop_loss_pct, s.stop_loss_price, s.source_final_prediction,
                       s.promoted_prediction, s.signal_day_close_1515,
                       s.entry_action, s.opening_gap_pct, s.call_reclaim_level,
                       s.status AS signal_status,
                       r.entry_price, r.entry_time, r.current_price,
                       r.current_quote_time, r.exit_price, r.exit_time, r.exit_reason,
                       r.pnl_points, r.pnl_per_lot, r.return_pct,
                       r.entry_charges, r.exit_charges, r.total_charges,
                       r.net_pnl_per_lot, r.net_return_pct,
                       r.status AS trade_status
                FROM "PaperExecutionSignal" s
                LEFT JOIN "PaperTradeResult" r
                  ON r.paper_execution_signal_id = s.id
                WHERE UPPER(s.symbol) = %s
                  AND s.model_version = %s
                  AND s.paper_trade_date = %s
                  AND COALESCE(r.status, s.status) = ANY(%s)
                ORDER BY s.signal_trade_date, s.id
                """,
                (symbol.upper(), model_version, trade_date, list(statuses)),
            )
            return [dict(r) for r in cur.fetchall()]

    def insert_paper_order(
        self,
        signal_id: int,
        order_role: str,
        side: str,
        quantity: int,
        requested_price: float | None,
        filled_price: float | None,
        status: str,
        payload: dict | None = None,
        error_message: str | None = None,
        paper_platform: str = "STOCKIE",
        quote_time: datetime | None = None,
        exchange: str = "NFO",
        variety: str = "regular",
        product: str = "NRML",
    ) -> int:
        self.ensure_paper_trade_tables()
        from psycopg2.extras import Json

        raw_payload = payload or {}
        depth = raw_payload.get("depth") if isinstance(raw_payload, dict) else {}
        depth = depth if isinstance(depth, dict) else {}
        bids = depth.get("buy") if isinstance(depth.get("buy"), list) else []
        asks = depth.get("sell") if isinstance(depth.get("sell"), list) else []
        best_bid = bids[0] if bids and isinstance(bids[0], dict) else {}
        best_ask = asks[0] if asks and isinstance(asks[0], dict) else {}
        bid_price = _float_or_none(best_bid.get("price"))
        ask_price = _float_or_none(best_ask.get("price"))
        spread = ask_price - bid_price if ask_price is not None and bid_price is not None else None

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "PaperOrder" (
                    paper_execution_signal_id, paper_platform, order_role, side,
                    exchange, variety, product, quantity,
                    requested_price, filled_price, market_bid_price,
                    market_bid_quantity, market_ask_price, market_ask_quantity,
                    bid_ask_spread, market_quote_time, status,
                    payload_json, error_message
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    signal_id, paper_platform, order_role, side,
                    exchange, variety, product, quantity,
                    requested_price, filled_price, bid_price,
                    _int_or_none(best_bid.get("quantity")), ask_price,
                    _int_or_none(best_ask.get("quantity")), spread, quote_time,
                    status, Json(raw_payload), error_message,
                ),
            )
            order_id = int(cur.fetchone()[0])
        self.conn.commit()
        return order_id

    def set_paper_execution_signal_status(
        self,
        signal_id: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "PaperExecutionSignal"
                SET status = %s,
                    error_message = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (status, error_message, signal_id),
            )
        self.conn.commit()

    def update_paper_order_charges(
        self,
        order_id: int,
        charge_result: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        """Persist Kite virtual contract-note charges for one paper fill."""
        from psycopg2.extras import Json

        result = charge_result or {}
        charges = result.get("charges") if isinstance(result.get("charges"), dict) else {}
        gst = charges.get("gst") if isinstance(charges.get("gst"), dict) else {}
        status = "CALCULATED" if charge_result is not None and error_message is None else "FAILED"
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "PaperOrder"
                SET transaction_tax = %s,
                    transaction_tax_type = %s,
                    exchange_turnover_charge = %s,
                    sebi_turnover_charge = %s,
                    brokerage = %s,
                    stamp_duty = %s,
                    gst_igst = %s,
                    gst_cgst = %s,
                    gst_sgst = %s,
                    gst_total = %s,
                    total_charges = %s,
                    charges_status = %s,
                    charges_calculated_at = now(),
                    charges_payload_json = %s,
                    charges_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    _float_or_none(charges.get("transaction_tax")),
                    charges.get("transaction_tax_type"),
                    _float_or_none(charges.get("exchange_turnover_charge")),
                    _float_or_none(charges.get("sebi_turnover_charge")),
                    _float_or_none(charges.get("brokerage")),
                    _float_or_none(charges.get("stamp_duty")),
                    _float_or_none(gst.get("igst")),
                    _float_or_none(gst.get("cgst")),
                    _float_or_none(gst.get("sgst")),
                    _float_or_none(gst.get("total")),
                    _float_or_none(charges.get("total")),
                    status,
                    Json(result),
                    error_message,
                    order_id,
                ),
            )
        self.conn.commit()

    def refresh_paper_trade_costs(self, signal_id: int) -> None:
        """Aggregate filled-order charges and calculate net paper P&L."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                WITH costs AS (
                    SELECT
                        COALESCE(SUM(total_charges) FILTER (WHERE order_role = 'ENTRY'), 0) AS entry_charges,
                        COALESCE(SUM(total_charges) FILTER (WHERE order_role = 'EXIT'), 0) AS exit_charges,
                        COALESCE(SUM(total_charges), 0) AS total_charges,
                        COUNT(*) FILTER (WHERE status = 'FILLED') AS filled_orders,
                        COUNT(*) FILTER (
                            WHERE status = 'FILLED' AND charges_status = 'CALCULATED'
                        ) AS charged_orders
                    FROM "PaperOrder"
                    WHERE paper_execution_signal_id = %s
                      AND status = 'FILLED'
                )
                UPDATE "PaperTradeResult" r
                SET entry_charges = costs.entry_charges,
                    exit_charges = costs.exit_charges,
                    total_charges = costs.total_charges,
                    net_pnl_per_lot = CASE
                        WHEN costs.filled_orders > 0
                         AND costs.filled_orders = costs.charged_orders
                        THEN r.pnl_per_lot - costs.total_charges
                    END,
                    net_return_pct = CASE
                        WHEN costs.filled_orders > 0
                         AND costs.filled_orders = costs.charged_orders
                         AND r.entry_price > 0
                         AND s.quantity > 0
                        THEN (r.pnl_per_lot - costs.total_charges)
                             / (r.entry_price * s.quantity) * 100
                    END,
                    updated_at = now()
                FROM costs, "PaperExecutionSignal" s
                WHERE r.paper_execution_signal_id = %s
                  AND s.id = r.paper_execution_signal_id
                """,
                (signal_id, signal_id),
            )
        self.conn.commit()

    def open_paper_trade(
        self,
        signal_id: int,
        entry_price: float,
        entry_time: datetime,
        payload: dict | None = None,
    ) -> int:
        self.ensure_paper_trade_tables()
        from psycopg2.extras import Json

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "PaperTradeResult" (
                    paper_execution_signal_id, entry_price, entry_time,
                    current_price, current_quote_time, pnl_points, pnl_per_lot,
                    return_pct, status, source
                )
                SELECT id, %s, %s, %s, %s, 0, 0, 0, 'OPEN', 'STOCKIE'
                FROM "PaperExecutionSignal"
                WHERE id = %s
                ON CONFLICT (paper_execution_signal_id) DO UPDATE SET
                    entry_price = EXCLUDED.entry_price,
                    entry_time = EXCLUDED.entry_time,
                    current_price = EXCLUDED.current_price,
                    current_quote_time = EXCLUDED.current_quote_time,
                    pnl_points = 0,
                    pnl_per_lot = 0,
                    return_pct = 0,
                    status = 'OPEN',
                    updated_at = now()
                RETURNING id
                """,
                (entry_price, entry_time, entry_price, entry_time, signal_id),
            )
            result_id = int(cur.fetchone()[0])
            cur.execute(
                """
                UPDATE "PaperExecutionSignal"
                SET status = 'OPEN',
                    target_1_price = CASE
                        WHEN target_1_pct IS NULL THEN target_1_price
                        ELSE %s * (1 + target_1_pct)
                    END,
                    target_2_price = NULL,
                    stop_loss_price = CASE
                        WHEN stop_loss_pct IS NULL THEN stop_loss_price
                        ELSE %s * (1 - stop_loss_pct)
                    END,
                    error_message = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (entry_price, entry_price, signal_id),
            )
            cur.execute(
                """
                INSERT INTO "PaperTradeEvent"
                    (paper_execution_signal_id, event_time, event_type, price, message, payload_json)
                VALUES (%s, %s, 'POSITION_OPENED', %s, 'Paper position opened', %s)
                """,
                (signal_id, entry_time, entry_price, Json(payload or {})),
            )
        self.conn.commit()
        return result_id

    def update_paper_trade_mtm(
        self,
        signal_id: int,
        current_price: float,
        current_time: datetime,
        entry_price: float,
        lot_size: int | None,
    ) -> None:
        points = current_price - entry_price
        pnl_per_lot = points * lot_size if lot_size else None
        return_pct = points / entry_price * 100 if entry_price else None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "PaperTradeResult"
                SET current_price = %s,
                    current_quote_time = %s,
                    pnl_points = %s,
                    pnl_per_lot = %s,
                    return_pct = %s,
                    updated_at = now()
                WHERE paper_execution_signal_id = %s
                  AND status = 'OPEN'
                """,
                (current_price, current_time, points, pnl_per_lot, return_pct, signal_id),
            )
        self.conn.commit()

    def ratchet_paper_trade_targets(
        self,
        signal_id: int,
        ratchet_price: float,
        ratchet_time: datetime,
        trigger: str,
        payload: dict | None = None,
    ):
        """Advance the cascade from the exact previous target price.

        The exact previous target becomes the next base. Stop widening uses the
        stored regime divider and the globally capped completed-target count.
        """
        from psycopg2.extras import Json
        from src.common.config import get_cascade_n_cap
        from src.execution.cascade import compute_cascade_levels

        with self.conn.cursor() as cur:
            cur.execute(
                '''SELECT target_1_price, target_1_pct, stop_loss_pct,
                          COALESCE(sl_divider, 10), COALESCE(completed_targets, 0)
                   FROM "PaperExecutionSignal" WHERE id=%s FOR UPDATE''',
                (signal_id,),
            )
            row = cur.fetchone()
            if not row or row[0] is None or row[1] is None or row[2] is None:
                return None
            completed_targets = int(row[4]) + 1
            levels = compute_cascade_levels(
                base_price=float(row[0]), completed_targets=completed_targets,
                target_pct=float(row[1]), sl_pct=float(row[2]),
                sl_divider=float(row[3]), n_cap=get_cascade_n_cap(),
            )
            cur.execute(
                """
                UPDATE "PaperExecutionSignal"
                SET target_1_price = %s,
                    target_2_price = NULL,
                    stop_loss_price = %s,
                    completed_targets = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (levels.target_price, levels.stop_loss_price, completed_targets, signal_id),
            )
            cur.execute(
                """
                INSERT INTO "PaperTradeEvent"
                    (paper_execution_signal_id, event_time, event_type, price, message, payload_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    signal_id, ratchet_time,
                    f"RATCHET_{trigger}",
                    ratchet_price,
                    f"Target {trigger} touched at {ratchet_price:.2f}; cascade advanced from prior target {levels.base_price:.2f}",
                    Json({**(payload or {}), "cascade": levels.__dict__}),
                ),
            )
        self.conn.commit()

    def set_paper_trade_quantity(self, signal_id: int, quantity: int) -> None:
        """Persist corpus-based execution quantity before creating the entry order."""
        with self.conn.cursor() as cur:
            cur.execute(
                'UPDATE "PaperExecutionSignal" SET quantity=%s, updated_at=now() WHERE id=%s',
                (int(quantity), signal_id),
            )
        self.conn.commit()
        return levels

    def close_paper_trade(
        self,
        signal_id: int,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
        entry_price: float,
        lot_size: int | None,
        payload: dict | None = None,
    ) -> None:
        from psycopg2.extras import Json

        points = exit_price - entry_price
        pnl_per_lot = points * lot_size if lot_size else None
        return_pct = points / entry_price * 100 if entry_price else None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "PaperTradeResult"
                SET current_price = %s,
                    current_quote_time = %s,
                    exit_price = %s,
                    exit_time = %s,
                    exit_reason = %s,
                    pnl_points = %s,
                    pnl_per_lot = %s,
                    return_pct = %s,
                    status = 'CLOSED',
                    updated_at = now()
                WHERE paper_execution_signal_id = %s
                """,
                (
                    exit_price, exit_time, exit_price, exit_time, exit_reason,
                    points, pnl_per_lot, return_pct, signal_id,
                ),
            )
            cur.execute(
                """
                UPDATE "PaperExecutionSignal"
                SET status = 'CLOSED', updated_at = now()
                WHERE id = %s
                """,
                (signal_id,),
            )
            cur.execute(
                """
                INSERT INTO "PaperTradeEvent"
                    (paper_execution_signal_id, event_time, event_type, price, message, payload_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    signal_id, exit_time, exit_reason, exit_price,
                    f"Paper position closed: {exit_reason}", Json(payload or {}),
                ),
            )
        self.conn.commit()

    def append_paper_trade_event(
        self,
        signal_id: int,
        event_type: str,
        price: float | None = None,
        message: str | None = None,
        payload: dict | None = None,
        event_time: datetime | None = None,
    ) -> None:
        from psycopg2.extras import Json

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "PaperTradeEvent"
                    (paper_execution_signal_id, event_time, event_type, price, message, payload_json)
                VALUES (%s, COALESCE(%s, now()), %s, %s, %s, %s)
                """,
                (signal_id, event_time, event_type, price, message, Json(payload or {})),
            )
        self.conn.commit()

    def upsert_news_articles(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        cols = [
            "article_id", "source", "url", "title", "summary", "published_at",
            "fetched_at", "region", "provider",
        ]
        with self.conn.cursor() as cur:
            cur.execute(SUPABASE_NEWS_SENTIMENT_SCHEMA_SQL)
            values = [tuple(r.get(c) for c in cols) for r in rows]
            execute_values(
                cur,
                f"""
                INSERT INTO "NewsArticle" ({", ".join(cols)})
                VALUES %s
                ON CONFLICT (article_id) DO UPDATE SET
                    source = EXCLUDED.source,
                    url = EXCLUDED.url,
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    published_at = EXCLUDED.published_at,
                    fetched_at = EXCLUDED.fetched_at,
                    region = EXCLUDED.region,
                    provider = EXCLUDED.provider,
                    updated_at = now()
                """,
                values,
            )
        self.conn.commit()
        return len(rows)

    def upsert_news_article_sentiments(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        cols = [
            "target_date", "article_id", "window_start", "window_end",
            "sentiment_label", "sentiment_score", "sentiment_confidence",
            "sentiment_model", "sectors", "sector_confidences", "sector_weight",
            "weighted_sentiment",
        ]
        update_cols = [c for c in cols if c not in ("target_date", "article_id")]
        set_clause = ",\n                    ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        with self.conn.cursor() as cur:
            cur.execute(SUPABASE_NEWS_SENTIMENT_SCHEMA_SQL)
            values = [tuple(r.get(c) for c in cols) for r in rows]
            execute_values(
                cur,
                f"""
                INSERT INTO "NewsArticleSentiment" ({", ".join(cols)})
                VALUES %s
                ON CONFLICT (target_date, article_id) DO UPDATE SET
                    {set_clause},
                    updated_at = now()
                """,
                values,
            )
        self.conn.commit()
        return len(rows)

    def upsert_nifty_market_sentiments(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        cols = [
            "target_date", "window_start", "window_end", "article_count",
            "usable_article_count", "composite_score", "composite_label",
            "mean_confidence", "positive_count", "neutral_count", "negative_count",
            "weighted_signal_sum", "normalization_denominator", "source_mix",
            "generated_at",
        ]
        update_cols = [c for c in cols if c != "target_date"]
        set_clause = ",\n                    ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        with self.conn.cursor() as cur:
            cur.execute(SUPABASE_NEWS_SENTIMENT_SCHEMA_SQL)
            values = [tuple(r.get(c) for c in cols) for r in rows]
            execute_values(
                cur,
                f"""
                INSERT INTO "NiftyMarketSentiment" ({", ".join(cols)})
                VALUES %s
                ON CONFLICT (target_date) DO UPDATE SET
                    {set_clause},
                    updated_at = now()
                """,
                values,
            )
        self.conn.commit()
        return len(rows)

    def upsert_global_index_ohlc(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        cols = [
            "index_code", "index_name", "yahoo_symbol", "region", "currency",
            "trade_date", "open_price", "high_price", "low_price", "close_price",
            "adj_close", "volume", "source", "fetched_at",
        ]
        key_cols = ("index_code", "trade_date", "source")
        update_cols = [c for c in cols if c not in key_cols]
        set_clause = ",\n                    ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

        with self.conn.cursor() as cur:
            cur.execute(SUPABASE_GLOBAL_INDEX_SCHEMA_SQL)
            values = [tuple(r.get(c) for c in cols) for r in rows]
            execute_values(
                cur,
                f"""
                INSERT INTO "GlobalIndexOhlc" ({", ".join(cols)})
                VALUES %s
                ON CONFLICT ON CONSTRAINT pk_global_index_ohlc DO UPDATE SET
                    {set_clause},
                    updated_at = now()
                """,
                values,
            )
        self.conn.commit()
        return len(rows)

    def get_latest_global_index_trade_date(self) -> date | None:
        with self.conn.cursor() as cur:
            cur.execute(SUPABASE_GLOBAL_INDEX_SCHEMA_SQL)
            cur.execute('SELECT max(trade_date) FROM "GlobalIndexOhlc"')
            row = cur.fetchone()
        self.conn.commit()
        return row[0] if row and row[0] else None

    def get_kite_access_token(self) -> str | None:
        with self.conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT access_token FROM \"KiteAccessToken\" ORDER BY updated_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
            except Exception:
                self.conn.rollback()
        return None


SUPABASE_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS "WatchedInstrument" (
    watched_id bigserial PRIMARY KEY,
    tradingsymbol varchar(50) NOT NULL,
    exchange varchar(20) NOT NULL,
    name varchar(200),
    instrument_token bigint,
    segment varchar(50),
    tick_size double precision,
    lot_size integer,
    instrument_type varchar(30) NOT NULL,
    sector varchar(100),
    industry varchar(100),
    is_fo_enabled boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz,
    CONSTRAINT uq_watched_instrument UNIQUE (tradingsymbol, exchange)
);

CREATE INDEX IF NOT EXISTS ix_watched_active
    ON "WatchedInstrument" (is_active, instrument_type, tradingsymbol);
CREATE INDEX IF NOT EXISTS ix_watched_token
    ON "WatchedInstrument" (instrument_token);

CREATE TABLE IF NOT EXISTS "UnderlyingSnapshot" (
    underlying varchar(50) NOT NULL,
    trade_date date NOT NULL,
    loaded_at timestamp NOT NULL,
    open_price double precision,
    high_price double precision,
    low_price double precision,
    close_price double precision,
    volume bigint,
    CONSTRAINT pk_underlying_snapshot PRIMARY KEY (underlying, trade_date)
);

CREATE TABLE IF NOT EXISTS "OptionInstrument" (
    id bigserial PRIMARY KEY,
    fetch_date date NOT NULL,
    instrument_token bigint NOT NULL,
    underlying varchar(50) NOT NULL,
    exchange varchar(20) NOT NULL,
    tradingsymbol varchar(100) NOT NULL,
    name varchar(100),
    strike double precision NOT NULL,
    expiry date NOT NULL,
    instrument_type varchar(10) NOT NULL,
    lot_size integer,
    tick_size double precision,
    segment varchar(50),
    CONSTRAINT uq_option_instrument_token UNIQUE (instrument_token)
);

CREATE INDEX IF NOT EXISTS ix_option_instrument_underlying
    ON "OptionInstrument" (underlying, expiry, strike, instrument_type);

CREATE TABLE IF NOT EXISTS "OptionSnapshot" (
    id bigserial PRIMARY KEY,
    option_instrument_id bigint NOT NULL REFERENCES "OptionInstrument"(id),
    snapshot_time timestamp NOT NULL,
    underlying_price double precision,
    last_price double precision,
    bid_price double precision,
    bid_qty integer,
    ask_price double precision,
    ask_qty integer,
    volume bigint,
    open_interest bigint,
    trade_date date NOT NULL,
    snapshot_label varchar(20) NOT NULL,
    exchange_timestamp timestamp,
    last_trade_time timestamp,
    last_quantity integer,
    average_price double precision,
    buy_quantity bigint,
    sell_quantity bigint,
    oi_day_high bigint,
    oi_day_low bigint,
    bid_orders integer,
    ask_orders integer,
    data_source varchar(50),
    CONSTRAINT uq_option_snapshot_instrument_date_label
        UNIQUE (option_instrument_id, trade_date, snapshot_label)
);

CREATE INDEX IF NOT EXISTS ix_option_snapshot_date_label
    ON "OptionSnapshot" (trade_date, snapshot_label);

CREATE TABLE IF NOT EXISTS "OptionSnapshotCalc" (
    option_snapshot_id bigint PRIMARY KEY REFERENCES "OptionSnapshot"(id) ON DELETE CASCADE,
    implied_volatility double precision,
    delta double precision,
    gamma double precision,
    theta double precision,
    vega double precision,
    valuation_price double precision,
    intrinsic_value double precision,
    time_value double precision,
    mid_price double precision,
    spread_width double precision,
    spread_width_pct double precision,
    days_to_expiry double precision,
    risk_free_rate double precision,
    calculation_status varchar(30),
    calculation_error varchar(500),
    created_at timestamp NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "SignalFeatureDaily" (
    feature_id bigserial PRIMARY KEY,
    signal_date date NOT NULL,
    symbol varchar(50) NOT NULL,
    feature_version varchar(50) NOT NULL DEFAULT 'v1',
    close_1515 double precision,
    open_915 double precision,
    high_day double precision,
    low_day double precision,
    volume_day bigint,
    ma10 double precision,
    ma20 double precision,
    ma50 double precision,
    ma90 double precision,
    rsi14 double precision,
    atr7 double precision,
    atr7_sma double precision,
    atr14 double precision,
    atr14_sma double precision,
    bb_upper double precision,
    bb_middle double precision,
    bb_lower double precision,
    bb_width double precision,
    ret_5d double precision,
    ret_10d double precision,
    ret_20d double precision,
    ret_60d double precision,
    volatility_10d double precision,
    volatility_20d double precision,
    volume_10d double precision,
    volume_20d double precision,
    trend_efficiency_5d double precision,
    trend_efficiency_10d double precision,
    trend_efficiency_20d double precision,
    trend_efficiency_60d double precision,
    relative_strength_vs_sector double precision,
    ma5d_slope double precision,
    ma10d_slope double precision,
    ma20_slope double precision,
    ma50_slope double precision,
    recent_high_5d double precision,
    recent_low_5d double precision,
    recent_high_10d double precision,
    recent_low_10d double precision,
    support_level_10d double precision,
    resistance_level_10d double precision,
    support_distance_10d double precision,
    resistance_distance_10d double precision,
    support_bounce_count_10d integer,
    resistance_rejection_count_10d integer,
    support_broken_10d boolean,
    resistance_broken_10d boolean,
    near_validated_support_10d boolean,
    near_validated_resistance_10d boolean,
    room_to_validated_resistance_10d double precision,
    recent_high_20d double precision,
    recent_low_20d double precision,
    range_position_5d double precision,
    range_position_10d double precision,
    range_position_20d double precision,
    regime varchar(30),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz,
    CONSTRAINT uq_signal_feature_daily UNIQUE (signal_date, symbol, feature_version)
);

CREATE INDEX IF NOT EXISTS ix_signal_feature_daily_symbol_date
    ON "SignalFeatureDaily" (symbol, signal_date);
CREATE INDEX IF NOT EXISTS ix_signal_feature_daily_regime
    ON "SignalFeatureDaily" (signal_date, regime);
"""


SUPABASE_NEWS_SENTIMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS "NewsArticle" (
    article_id   varchar(64) PRIMARY KEY,
    source       varchar(200),
    url          text,
    title        text,
    summary      text,
    published_at timestamptz,
    fetched_at   timestamptz,
    region       varchar(50),
    provider     varchar(50),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_news_article_published_at
    ON "NewsArticle" (published_at);
CREATE INDEX IF NOT EXISTS ix_news_article_provider_region
    ON "NewsArticle" (provider, region);

CREATE TABLE IF NOT EXISTS "NewsArticleSentiment" (
    target_date               date NOT NULL,
    article_id                varchar(64) NOT NULL,
    window_start              timestamptz,
    window_end                timestamptz,
    sentiment_label           varchar(20),
    sentiment_score           double precision,
    sentiment_confidence      double precision,
    sentiment_model           varchar(100),
    sectors                   text,
    sector_confidences        text,
    sector_weight             double precision,
    weighted_sentiment        double precision,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_news_article_sentiment PRIMARY KEY (target_date, article_id),
    CONSTRAINT fk_news_article_sentiment_article
        FOREIGN KEY (article_id) REFERENCES "NewsArticle"(article_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_news_article_sentiment_target_date
    ON "NewsArticleSentiment" (target_date);
CREATE INDEX IF NOT EXISTS ix_news_article_sentiment_label
    ON "NewsArticleSentiment" (target_date, sentiment_label);

CREATE TABLE IF NOT EXISTS "NiftyMarketSentiment" (
    target_date                date PRIMARY KEY,
    window_start               timestamptz,
    window_end                 timestamptz,
    article_count              integer,
    usable_article_count       integer,
    composite_score            double precision,
    composite_label            varchar(20),
    mean_confidence            double precision,
    positive_count             integer,
    neutral_count              integer,
    negative_count             integer,
    weighted_signal_sum        double precision,
    normalization_denominator  double precision,
    source_mix                 text,
    generated_at               timestamptz,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_nifty_market_sentiment_label
    ON "NiftyMarketSentiment" (target_date, composite_label);
"""


SUPABASE_GLOBAL_INDEX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS "GlobalIndexOhlc" (
    index_code    varchar(50) NOT NULL,
    index_name    varchar(120) NOT NULL,
    yahoo_symbol  varchar(50) NOT NULL,
    region        varchar(50),
    currency      varchar(10),
    trade_date    date NOT NULL,
    open_price    double precision,
    high_price    double precision,
    low_price     double precision,
    close_price   double precision,
    adj_close     double precision,
    volume        bigint,
    source        varchar(50) NOT NULL DEFAULT 'yfinance',
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_global_index_ohlc PRIMARY KEY (index_code, trade_date, source)
);

CREATE INDEX IF NOT EXISTS ix_global_index_ohlc_date
    ON "GlobalIndexOhlc" (trade_date);
CREATE INDEX IF NOT EXISTS ix_global_index_ohlc_symbol_date
    ON "GlobalIndexOhlc" (index_code, trade_date);
"""
