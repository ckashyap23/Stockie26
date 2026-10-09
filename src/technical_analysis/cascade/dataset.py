"""Feature dataset assembly + labelling for the cascade.

Loads all SignalFeatureDaily rows from the DB, joins India VIX, and derives
the common-threshold actual_trade_label. Read-only w.r.t. the DB.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.config import (
    get_settings,
    get_strategy_feature_cleanup_enabled,
    get_underlying_lookback_days,
)
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.db.supabase_client import SupabaseDatabaseClient

from .constants import (
    CALL,
    PUT,
    FLAT,
    TARGET_THRESHOLD,
    RELAXED_TARGET_THRESHOLD,
    _VIX_COLS,
    _BASE_STR_COLS,
)
from .global_index_features import add_global_index_features


def _monthly_expiry_from_trading_days(month_days: list[pd.Timestamp]) -> pd.Timestamp | None:
    if not month_days:
        return None
    year = int(month_days[0].year)
    month = int(month_days[0].month)
    month_end = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(1)
    # Monday=0 ... Thursday=3.
    offset = (month_end.weekday() - 3) % 7
    last_thursday = month_end - pd.Timedelta(days=offset)
    eligible = [d for d in month_days if d <= last_thursday]
    return eligible[-1] if eligible else month_days[-1]


def _compute_is_rollover_day(signal_dates: pd.Series, window_days: int = 3) -> pd.Series:
    """True when signal_date is within 3 prior trading days of monthly expiry (inclusive)."""
    dt = pd.to_datetime(signal_dates, errors="coerce")
    out = pd.Series(False, index=signal_dates.index)
    valid = dt.dropna().sort_values().drop_duplicates()
    if valid.empty:
        return out

    by_month = valid.groupby([valid.dt.year, valid.dt.month])
    rollover_set: set[pd.Timestamp] = set()
    for _, month_series in by_month:
        month_days = list(month_series)
        expiry_day = _monthly_expiry_from_trading_days(month_days)
        if expiry_day is None:
            continue
        idx = month_days.index(expiry_day)
        start = max(0, idx - window_days)
        rollover_set.update(month_days[start : idx + 1])

    return dt.isin(list(rollover_set)).fillna(False)


def _ensure_atr_features(df: pd.DataFrame) -> pd.DataFrame:
    """Supply ATR7/ATR14 fields for stores created before their DB backfills."""
    needed = ("atr7", "atr7_sma", "atr14_sma")
    if all(col in df and df[col].notna().all() for col in needed):
        return df
    out = df.sort_values("signal_date").reset_index(drop=True).copy()
    high = pd.to_numeric(out["high_day"], errors="coerce")
    low = pd.to_numeric(out["low_day"], errors="coerce")
    close = pd.to_numeric(out["close_1515"], errors="coerce")
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    calculated = {
        "atr7": true_range.ewm(alpha=1.0 / 7, adjust=False).mean(),
        "atr7_sma": true_range.rolling(7, min_periods=7).mean(),
        "atr14_sma": true_range.rolling(14, min_periods=14).mean(),
    }
    for col, values in calculated.items():
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(values)
        else:
            out[col] = values
    return out


def _label_at(df: pd.DataFrame, threshold: float) -> np.ndarray:
    """Touch-based CALL/PUT/BOTH/NO_POSITION label over trade_horizon_days sessions.

    Uses future_high_nd / future_low_nd (max high / min low over n sessions) when
    available; falls back to next_high / next_low for single-day compatibility.
    Entry reference is always next_open (D+1 open price).
    """
    o = df["next_open"]
    h = df["future_high_nd"] if "future_high_nd" in df.columns else df["next_high"]
    lo = df["future_low_nd"] if "future_low_nd" in df.columns else df["next_low"]
    call_ok = (h - o) / o >= threshold
    put_ok = (o - lo) / o >= threshold
    return np.select(
        [call_ok & ~put_ok, put_ok & ~call_ok, call_ok & put_ok],
        [CALL, PUT, "BOTH"],
        default=FLAT,
    )


def _call_ok(
    df: pd.DataFrame,
    label_column: str = "actual_trade_label",
) -> pd.Series:
    return df[label_column].isin([CALL, "BOTH"])


def _put_ok(
    df: pd.DataFrame,
    label_column: str = "actual_trade_label",
) -> pd.Series:
    return df[label_column].isin([PUT, "BOTH"])


def load_vix() -> pd.DataFrame:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                'SELECT factor_date, india_vix FROM "MacroFactorDaily" '
                "WHERE india_vix IS NOT NULL ORDER BY factor_date"
            )
            rows = cur.fetchall()
    finally:
        db.close()
    vix = pd.DataFrame(rows, columns=["signal_date", "vix_close"])
    vix["signal_date"] = pd.to_datetime(vix["signal_date"]).dt.strftime("%Y-%m-%d")
    vix["vix_close"] = vix["vix_close"].astype(float)
    vix["vix_chg_1d"] = vix["vix_close"].diff()
    vix["vix_chg_pct"] = vix["vix_close"].pct_change()
    return vix


def _load_feature_rows_from_db() -> pd.DataFrame:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                'SELECT * FROM "SignalFeatureDaily" '
                "WHERE symbol = %s ORDER BY signal_date",
                ("NIFTY",),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        db.close()

    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        raise RuntimeError('No NIFTY rows found in "SignalFeatureDaily" for DB-backed prediction.')

    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("signal_date").reset_index(drop=True)
    df["next_trade_date"] = df["signal_date"].shift(-1)
    # Fill next_trade_date for the last row from TradingCalendar (shift gives NaT there)
    if pd.isna(df.loc[df.index[-1], "next_trade_date"]):
        try:
            from datetime import date as _date
            last_signal_date = pd.to_datetime(df.loc[df.index[-1], "signal_date"]).date()
            settings = get_settings()
            cal_db = SupabaseDatabaseClient(settings) if settings.supabase_conn_str else get_database_client(settings)
            cal_db.connect()
            try:
                nxt = cal_db.get_next_trading_day(last_signal_date, exchange="NSE")
            finally:
                cal_db.close()
            if nxt is not None:
                df.loc[df.index[-1], "next_trade_date"] = str(nxt)
        except Exception as _exc:  # noqa: BLE001
            pass  # leave as NaT; _frame_to_rows will skip this row
    df["next_open"] = df["open_915"].shift(-1)
    df["next_high"] = df["high_day"].shift(-1)
    df["next_low"] = df["low_day"].shift(-1)
    df["next_close"] = df["close_1515"].shift(-1)
    df["next_return_pct"] = (df["next_close"] - df["close_1515"]) / df["close_1515"]
    support_level = df["support_level_10d"] if "support_level_10d" in df else df["recent_low_10d"]
    resistance_level = df["resistance_level_10d"] if "resistance_level_10d" in df else df["recent_high_10d"]
    df["support_10d"] = pd.to_numeric(support_level, errors="coerce").fillna(df["recent_low_10d"])
    df["resistance_10d"] = pd.to_numeric(resistance_level, errors="coerce").fillna(df["recent_high_10d"])
    computed_support_distance = (df["close_1515"] - df["support_10d"]) / df["atr14"].replace(0, np.nan)
    computed_resistance_distance = (df["resistance_10d"] - df["close_1515"]) / df["close_1515"]
    if "support_distance_10d" in df:
        df["support_distance_10d"] = pd.to_numeric(df["support_distance_10d"], errors="coerce").fillna(
            computed_support_distance
        )
    else:
        df["support_distance_10d"] = computed_support_distance
    if "resistance_distance_10d" in df:
        df["resistance_distance_10d"] = pd.to_numeric(df["resistance_distance_10d"], errors="coerce").fillna(
            computed_resistance_distance
        )
    else:
        df["resistance_distance_10d"] = computed_resistance_distance
    computed_recent_high_40d = df["high_day"].shift(1).rolling(window=40, min_periods=1).max()
    computed_resistance_distance_40d = (computed_recent_high_40d - df["close_1515"]) / df["close_1515"]
    if "resistance_distance_40d" in df:
        df["resistance_distance_40d"] = pd.to_numeric(df["resistance_distance_40d"], errors="coerce").fillna(
            computed_resistance_distance_40d
        )
    else:
        df["resistance_distance_40d"] = computed_resistance_distance_40d
    # Derived-feature fallbacks keep older feature rows compatible with the
    # current strategy/diagnostic contract until their persisted values are backfilled.
    computed_volume_hybrid = df["volume_day"] / df["volume_20d"].replace(0, np.nan)
    if "volume_hybrid" in df:
        df["volume_hybrid"] = pd.to_numeric(df["volume_hybrid"], errors="coerce").fillna(computed_volume_hybrid)
    else:
        df["volume_hybrid"] = computed_volume_hybrid
    computed_slope_combo = (
        0.50 * df["ma5d_slope"]
        + 0.30 * df["ma10d_slope"]
        + 0.20 * df["ma20_slope"]
    )
    if "ma_slope_combo" in df:
        df["ma_slope_combo"] = pd.to_numeric(df["ma_slope_combo"], errors="coerce").fillna(computed_slope_combo)
    else:
        df["ma_slope_combo"] = computed_slope_combo
    df = df[[c for c in df.columns if c not in _VIX_COLS and c != "regime"]]
    for col in df.columns:
        if col not in _BASE_STR_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _cleanup_strategy_facing_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a strategy-facing feature frame with canonical, non-redundant columns.

    This cleanup is intentionally non-destructive: it only changes the in-memory
    frame used by prediction/analytics, not the persisted DB schema.
    """
    out = df.copy()

    # Keep canonical support/resistance names and drop duplicate aliases.
    duplicate_aliases = [
        "support_level_10d",
        "resistance_level_10d",
    ]

    # Remove raw bookkeeping metadata from strategy-facing frames.
    metadata_cols = [
        "feature_id",
        "feature_version",
        "symbol",
        "created_at",
        "updated_at",
    ]

    # Keep rollups + breadth/risk flags; remove raw per-index return inputs.
    raw_global_cols = [
        "global_ret_ASX200",
        "global_ret_CAC40",
        "global_ret_DAX",
        "global_ret_DOW",
        "global_ret_FTSE100",
        "global_ret_HANG_SENG",
        "global_ret_KOSPI",
        "global_ret_NASDAQ",
        "global_ret_NIKKEI225",
        "global_ret_RUSSELL2000",
        "global_ret_SHANGHAI",
        "global_ret_SP500",
        "global_ret_NIFTY50",
        "global_ret_SENSEX",
        "global_ret_INDIA_VIX",
    ]

    # Keep one breadth representation in the strategy-facing frame.
    redundant_breadth_cols = [
        "global_positive_count",
        "global_negative_count",
    ]

    # ma_slope_combo is a deterministic weighted blend of the component slopes.
    # Keep the components to avoid double-weighting the same signal.
    redundant_derived_cols = [
        "ma_slope_combo",
    ]

    drop_cols = (
        duplicate_aliases
        + metadata_cols
        + raw_global_cols
        + redundant_breadth_cols
        + redundant_derived_cols
    )
    existing = [col for col in drop_cols if col in out.columns]
    if existing:
        out = out.drop(columns=existing)

    return out


def build_base() -> pd.DataFrame:
    """Load SignalFeatureDaily rows from the DB and derive actual_trade_label."""
    df = _load_feature_rows_from_db()

    df = df.merge(load_vix(), on="signal_date", how="left")
    df["is_rollover_day"] = _compute_is_rollover_day(df["signal_date"])
    df = add_global_index_features(df)
    df = _ensure_atr_features(df)

    # Multi-day future extremes: max high / min low over the next UNDERLYING_LOOKBACK_DAYS
    # sessions. next_open is still D+1 open; the touch threshold is checked over n days.
    n = get_underlying_lookback_days()
    future_highs = pd.concat(
        [df["high_day"].shift(-step) for step in range(1, n + 1)], axis=1
    )
    future_lows = pd.concat(
        [df["low_day"].shift(-step) for step in range(1, n + 1)], axis=1
    )
    df["future_high_nd"] = future_highs.max(axis=1)
    df["future_low_nd"] = future_lows.min(axis=1)

    # actual_trade_label: common threshold from NIFTY_TARGET_PCT.\r\n    # from STRESS_NIFTY_TARGET_PCT / CALM_NIFTY_TARGET_PCT in .env.
    # Entry = next_open; look-ahead = future_high_nd / future_low_nd over
    # UNDERLYING_LOOKBACK_DAYS sessions.
    _o  = pd.to_numeric(df["next_open"], errors="coerce").replace(0, float("nan"))
    _h  = pd.to_numeric(
        df["future_high_nd"] if "future_high_nd" in df.columns else df["next_high"],
        errors="coerce",
    )
    _lo = pd.to_numeric(
        df["future_low_nd"] if "future_low_nd" in df.columns else df["next_low"],
        errors="coerce",
    )
    _call_ok_lbl = (_h  - _o) / _o >= TARGET_THRESHOLD
    _put_ok_lbl  = (_o  - _lo) / _o >= TARGET_THRESHOLD
    # Only label rows where the COMPLETE future window is available.
    # Rows with partial or missing future data (last n trading days) get NULL
    # so they are not mis-scored as NO_POSITION in metrics.
    n_cols = n  # matches the horizon used above
    future_highs_cols = pd.concat(
        [df["high_day"].shift(-step) for step in range(1, n_cols + 1)], axis=1
    )
    future_lows_cols = pd.concat(
        [df["low_day"].shift(-step) for step in range(1, n_cols + 1)], axis=1
    )
    _complete_future = (
        future_highs_cols.notna().all(axis=1)
        & future_lows_cols.notna().all(axis=1)
        & _o.notna()
    )
    df["actual_trade_label"] = pd.Series(
        np.select(
            [_call_ok_lbl & ~_put_ok_lbl, _put_ok_lbl & ~_call_ok_lbl, _call_ok_lbl & _put_ok_lbl],
            [CALL, PUT, "BOTH"],
            default=FLAT,
        ),
        index=df.index,
        dtype=object,
    ).where(_complete_future, other=None)
    _call_ok_relaxed = (_h - _o) / _o >= RELAXED_TARGET_THRESHOLD
    _put_ok_relaxed = (_o - _lo) / _o >= RELAXED_TARGET_THRESHOLD
    df["actual_trade_label_relaxed"] = pd.Series(
        np.select(
            [
                _call_ok_relaxed & ~_put_ok_relaxed,
                _put_ok_relaxed & ~_call_ok_relaxed,
                _call_ok_relaxed & _put_ok_relaxed,
            ],
            [CALL, PUT, "BOTH"],
            default=FLAT,
        ),
        index=df.index,
        dtype=object,
    ).where(_complete_future, other=None)

    if get_strategy_feature_cleanup_enabled():
        df = _cleanup_strategy_facing_features(df)

    return df


def scoring_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy labelled with the common target threshold for strategy scoring."""
    sub = df.copy()
    sub["actual_trade_label"] = _label_at(sub, TARGET_THRESHOLD)
    return sub
