"""Feature dataset assembly + labelling for the cascade.

Reads the canonical feature store (BASE_CSV), appends any newer SignalFeatureDaily
rows that already have a realised next-day outcome, joins India VIX, routes each
day into the calm/stress volatility regime and derives the regime-aware
actual_trade_label. Read-only w.r.t. the DB except for SELECTing India VIX and the
recent feature rows.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from src.common.config import get_settings, get_trade_horizon_days
from src.data_manager.db.client_factory import get_database_client

from .constants import (
    FEATURE_STORE, CALL, PUT, FLAT,
    REGIME_CALM, REGIME_STRESS, REGIME_VIX_CUTOFF, REGIME_VOL_CUTOFF, REGIME_THRESHOLD,
    _DROP_EXACT, _VIX_COLS, _BASE_STR_COLS,
)
from .global_index_features import add_global_index_features


def _ensure_atr14_sma(df: pd.DataFrame) -> pd.DataFrame:
    """Supply the new feature for CSV stores created before its DB backfill."""
    if "atr14_sma" in df and df["atr14_sma"].notna().all():
        return df
    out = df.sort_values("trade_date").reset_index(drop=True).copy()
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
    vix = pd.DataFrame(rows, columns=["trade_date", "vix_close"])
    vix["trade_date"] = pd.to_datetime(vix["trade_date"]).dt.strftime("%Y-%m-%d")
    vix["vix_close"] = vix["vix_close"].astype(float)
    vix["vix_chg_1d"] = vix["vix_close"].diff()
    vix["vix_chg_pct"] = vix["vix_close"].pct_change()
    return vix


def _load_recent_feature_rows(existing: pd.DataFrame) -> pd.DataFrame:
    """Pull any SignalFeatureDaily NIFTY rows newer than the latest base date and
    shape them into the (already column-stripped) base schema so they flow through
    the whole pipeline. Only dates that already have a realized next-day candle are
    returned (a date is scorable only once D+1 exists); the newest still-open day
    is therefore held back until its outcome lands. Returns an empty frame (matching
    `existing` columns) when there is nothing new or the DB is unavailable."""
    max_date = str(existing["trade_date"].max())
    try:
        settings = get_settings()
        db = get_database_client(settings)
        db.connect()
        try:
            with db.conn.cursor() as cur:
                cur.execute(
                    'SELECT * FROM "SignalFeatureDaily" '
                    "WHERE symbol = %s AND signal_date >= %s ORDER BY signal_date",
                    ("NIFTY", max_date),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - never let a DB hiccup break the rebuild
        print(f"[WARN] recent-row append skipped: {exc}")
        return existing.iloc[0:0].copy()

    sf = pd.DataFrame(rows, columns=cols)
    if sf.empty:
        return existing.iloc[0:0].copy()

    sf = sf.rename(columns={"signal_date": "trade_date"})
    sf["trade_date"] = pd.to_datetime(sf["trade_date"]).dt.strftime("%Y-%m-%d")
    sf = sf.sort_values("trade_date").reset_index(drop=True)

    # realized D+1 outcomes (used only for grading), from the next chronological row
    sf["next_trade_date"] = sf["trade_date"].shift(-1)
    sf["next_open"] = sf["open_915"].shift(-1)
    sf["next_high"] = sf["high_day"].shift(-1)
    sf["next_low"] = sf["low_day"].shift(-1)
    sf["next_close"] = sf["close_1515"].shift(-1)
    sf["next_return_pct"] = (sf["next_close"] - sf["close_1515"]) / sf["close_1515"]

    # support/resistance levels + distances derived from the 10-day extremes
    sf["support_10d"] = sf["recent_low_10d"]
    sf["resistance_10d"] = sf["recent_high_10d"]
    sf["support_distance_10d"] = (sf["close_1515"] - sf["support_10d"]) / sf["close_1515"]
    sf["resistance_distance_10d"] = (sf["resistance_10d"] - sf["close_1515"]) / sf["close_1515"]

    # keep only genuinely new, scorable dates (a realized next-day candle exists)
    sf = sf[(sf["trade_date"] > max_date) & sf["next_open"].notna()]
    if sf.empty:
        return existing.iloc[0:0].copy()

    out = sf.reindex(columns=existing.columns)
    for col in out.columns:
        if col not in _BASE_STR_COLS:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.reset_index(drop=True)


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

    df = df.rename(columns={"signal_date": "trade_date"})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["next_trade_date"] = df["trade_date"].shift(-1)
    # Fill next_trade_date for the last row from TradingCalendar (shift gives NaT there)
    if pd.isna(df.loc[df.index[-1], "next_trade_date"]):
        try:
            from datetime import date as _date
            last_trade_date = pd.to_datetime(df.loc[df.index[-1], "trade_date"]).date()
            cal_db = get_database_client(get_settings())
            cal_db.connect()
            try:
                nxt = cal_db.get_next_trading_day(last_trade_date, exchange="NSE")
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


def _load_seed_feature_rows() -> tuple[pd.DataFrame, bool]:
    source = os.getenv("NIFTY_PREDICTION_FEATURE_SOURCE", "auto").strip().lower()
    if source not in {"auto", "csv", "db"}:
        raise ValueError("NIFTY_PREDICTION_FEATURE_SOURCE must be one of: auto, csv, db")

    if source == "db" or (source == "auto" and not FEATURE_STORE.exists()):
        return _load_feature_rows_from_db(), True

    df = pd.read_csv(FEATURE_STORE)
    df = df[[c for c in df.columns
             if c not in _DROP_EXACT
             and not c.startswith("strategy_")
             and c not in _VIX_COLS
             and c != "regime"]]
    return df, False


def build_base() -> pd.DataFrame:
    """Read the current base.csv, strip regime/strategy/label columns, join VIX,
    and (re)derive actual_trade_label from the 0.5% intraday rule.

    Idempotent: safe to re-run on an already-restructured base.csv because all
    feature + next_* columns are retained.
    """
    df, loaded_from_db = _load_seed_feature_rows()

    # Append any newer SignalFeatureDaily rows (frozen here = base.csv max date),
    # so the recent dates flow through regime/label/signal/cascade and are persisted
    # back into the feature store on write. New rows are graded with the same rules.
    if not loaded_from_db:
        recent = _load_recent_feature_rows(df)
        if not recent.empty:
            df = pd.concat([df, recent], ignore_index=True)
            print(f"  appended {len(recent)} new dated row(s): "
                  f"{', '.join(recent['trade_date'])}")

    df = df.merge(load_vix(), on="trade_date", how="left")
    df = add_global_index_features(df)
    df = _ensure_atr14_sma(df)

    # Volatility regime first, then a regime-aware label: stress rows are graded
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
