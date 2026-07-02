"""Realised three-session signal quality metrics.

These columns are outcomes and must only be attached after a strategy has
generated its signal.  Using them as strategy inputs would introduce lookahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CALL = "CALL"
PUT = "PUT"


def add_raw_direction(
    df: pd.DataFrame,
    *,
    close_col: str = "close_1515",
    high_col: str = "high_day",
    low_col: str = "low_day",
    atr_col: str = "atr14_sma",
    horizon: int | None = None,
) -> pd.DataFrame:
    """Add future excursions and bounded raw direction for a complete horizon.

    raw_direction = (bull_score - bear_score) / (bull_score + bear_score)
    and lies in [-1, 1]. The last ``horizon`` rows remain unscored.
    """
    if horizon is None:
        from src.common.config import get_underlying_lookback_days
        horizon = get_underlying_lookback_days()
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    required = {close_col, high_col, low_col, atr_col}
    missing = sorted(required.difference(df.columns))
    if missing:
        raise KeyError(f"Missing signal-strength columns: {', '.join(missing)}")

    out = df.copy()
    close = pd.to_numeric(out[close_col], errors="coerce")
    atr = pd.to_numeric(out[atr_col], errors="coerce")
    future_highs = pd.concat(
        [pd.to_numeric(out[high_col], errors="coerce").shift(-step) for step in range(1, horizon + 1)],
        axis=1,
    )
    future_lows = pd.concat(
        [pd.to_numeric(out[low_col], errors="coerce").shift(-step) for step in range(1, horizon + 1)],
        axis=1,
    )
    complete = future_highs.notna().all(axis=1) & future_lows.notna().all(axis=1) & atr.gt(0) & close.notna()

    out["future_high_3d"] = future_highs.max(axis=1).where(complete)
    out["future_low_3d"] = future_lows.min(axis=1).where(complete)
    bull = ((out["future_high_3d"] - close) / atr).clip(lower=0).where(complete)
    bear = ((close - out["future_low_3d"]) / atr).clip(lower=0).where(complete)
    denominator = bull + bear
    raw = ((bull - bear) / denominator).where(denominator.gt(0))
    out["bull_score"] = bull
    out["bear_score"] = bear
    out["raw_direction"] = raw
    return out


def signal_quality(signal: pd.Series, raw_direction: pd.Series) -> pd.Series:
    """Align realised raw direction to CALL/PUT signals; non-signals are NaN."""
    aligned_signal = signal.reindex(raw_direction.index)
    return pd.Series(
        np.select(
            [aligned_signal.eq(CALL), aligned_signal.eq(PUT)],
            [raw_direction, -raw_direction],
            default=np.nan,
        ),
        index=raw_direction.index,
        dtype=float,
        name="signal_quality",
    )


def summarize_signal_quality(signal: pd.Series, outcomes: pd.DataFrame) -> dict[str, float | int | None]:
    """Aggregate quality over scorable CALL/PUT fires only."""
    quality = signal_quality(signal, outcomes["raw_direction"])
    fired = signal.isin([CALL, PUT]).reindex(outcomes.index, fill_value=False)
    scored = quality[fired & quality.notna()]
    return {
        "quality_scored_fires": int(scored.size),
        "mean_signal_quality": round(float(scored.mean()), 6) if not scored.empty else None,
        "median_signal_quality": round(float(scored.median()), 6) if not scored.empty else None,
        "positive_quality_rate_pct": round(float(scored.gt(0).mean() * 100), 1) if not scored.empty else None,
    }
