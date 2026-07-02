"""Feature dataset assembly + labelling for the cascade.

Loads all SignalFeatureDaily rows from the DB, joins India VIX, routes each
day into the calm/stress volatility regime and derives the regime-aware
actual_trade_label. Read-only w.r.t. the DB.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.config import get_settings, get_trade_horizon_days
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.db.supabase_client import SupabaseDatabaseClient

from .constants import (
    CALL, PUT, FLAT,
    REGIME_CALM, REGIME_STRESS, REGIME_VIX_CUTOFF, REGIME_VOL_CUTOFF, REGIME_THRESHOLD,
    _VIX_COLS, _BASE_STR_COLS,
)
from .global_index_features import add_global_index_features


def _ensure_atr14_sma(df: pd.DataFrame) -> pd.DataFrame:
    """Supply the new feature for CSV stores created before its DB backfill."""
    if "atr14_sma" in df and df["atr14_sma"].notna().all():
        return df
    out = df.sort_values("signal_date").reset_index(drop=True).copy()
    high = pd.to_numeric(out["high_day"], errors="coerce")
    low = pd.to_numeric(out["low_day"], errors="coerce")
    close = pd.to_numeric(out["close_1515"], errors="coerce")
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    calculated = true_range.rolling(14, min_periods=14).mean()
    if "atr14_sma" in out:
        out["atr14_sma"] = pd.to_numeric(out["atr14_sma"], errors="coerce").fillna(calculated)
    else:
        out["atr14_sma"] = calculated
    return out


def classify_regime(df: pd.DataFrame) -> pd.Series:
    """Route each trade_date into the calm or stress volatility regime using
    only same-day features (no lookahead): calm = low India VIX AND low realised
    10-day volatility; everything else is stress."""
    calm = (df["vix_close"] < REGIME_VIX_CUTOFF) & (df["volatility_10d"] < REGIME_VOL_CUTOFF)
    return pd.Series(np.where(calm.fillna(False), REGIME_CALM, REGIME_STRESS), index=df.index)


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


def _call_ok(df: pd.DataFrame) -> pd.Series:
    return df["actual_trade_label"].isin([CALL, "BOTH"])


def _put_ok(df: pd.DataFrame) -> pd.Series:
    return df["actual_trade_label"].isin([PUT, "BOTH"])


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
    df["support_10d"] = df["recent_low_10d"]
    df["resistance_10d"] = df["recent_high_10d"]
    df["support_distance_10d"] = (df["close_1515"] - df["support_10d"]) / df["close_1515"]
    df["resistance_distance_10d"] = (df["resistance_10d"] - df["close_1515"]) / df["close_1515"]
    df = df[[c for c in df.columns if c not in _VIX_COLS and c != "regime"]]
    for col in df.columns:
        if col not in _BASE_STR_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build_base() -> pd.DataFrame:
    """Load all SignalFeatureDaily rows from the DB, join VIX, classify regimes,
    and derive actual_trade_label. Always reads from the DB — no CSV fallback.
    """
    df = _load_feature_rows_from_db()

    df = df.merge(load_vix(), on="signal_date", how="left")
    df = add_global_index_features(df)
    df = _ensure_atr14_sma(df)

    # Classify volatility regime, then a regime-aware label: stress rows are graded
    # at 0.5% and calm rows at 0.3% (calm days rarely print a 0.5% move).
    df["regime"] = classify_regime(df)

    # Multi-day future extremes: max high / min low over the next trade_horizon_days
    # sessions. next_open is still D+1 open; the touch threshold is checked over n days.
    n = get_trade_horizon_days()
    future_highs = pd.concat(
        [df["high_day"].shift(-step) for step in range(1, n + 1)], axis=1
    )
    future_lows = pd.concat(
        [df["low_day"].shift(-step) for step in range(1, n + 1)], axis=1
    )
    df["future_high_nd"] = future_highs.max(axis=1)
    df["future_low_nd"] = future_lows.min(axis=1)

    lab = pd.Series(FLAT, index=df.index, dtype=object)
    for regime, th in REGIME_THRESHOLD.items():
        mask = df["regime"] == regime
        lab.loc[mask] = _label_at(df.loc[mask], th)
    df["actual_trade_label"] = lab
    return df


def regime_frame(df: pd.DataFrame, regime: str) -> pd.DataFrame:
    """Subset to one regime and (re)label it at that regime's threshold, so
    strategy scoring inside the regime uses the regime-appropriate move size."""
    sub = df[df["regime"] == regime].copy()
    sub["actual_trade_label"] = _label_at(sub, REGIME_THRESHOLD[regime])
    return sub
