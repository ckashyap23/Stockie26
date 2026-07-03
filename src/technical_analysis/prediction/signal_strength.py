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
    atr_col: str = "atr14_sma",
    horizon: int | None = None,
) -> pd.DataFrame:
    """Add future excursions and bounded raw direction for a complete horizon.

    raw_signal_quality = (bull_score - bear_score) / (bull_score + bear_score)
    and lies in [-1, 1]. The last ``horizon`` rows remain unscored.
    """
    if horizon is None:
        from src.common.config import get_underlying_lookback_days
        horizon = get_underlying_lookback_days()
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    close_col = "close_1515"
    high_col = "high_day"
    low_col = "low_day"
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
    out["raw_signal_quality"] = raw
    out["actual_quality_label"] = pd.Series(
        np.select(
            [bull.gt(0.5) & raw.gt(0), bear.gt(0.5) & raw.lt(0)],
            [CALL, PUT],
            default="NO_POSITION",
        ),
        index=out.index,
        dtype=object,
    ).where(complete, "NO_POSITION")
    return out


def signal_quality(signal: pd.Series, raw_signal_quality: pd.Series) -> pd.Series:
    """Align realised raw direction to CALL/PUT signals; non-signals are NaN."""
    aligned_signal = signal.reindex(raw_signal_quality.index)
    return pd.Series(
        np.select(
            [aligned_signal.eq(CALL), aligned_signal.eq(PUT)],
            [raw_signal_quality, -raw_signal_quality],
            default=np.nan,
        ),
        index=raw_signal_quality.index,
        dtype=float,
        name="signal_quality",
    )


def quality_label_metrics(
    signal: pd.Series,
    actual_quality_label: pd.Series,
    *,
    side: str | None = None,
) -> dict[str, float | int | None]:
    """Precision/recall/F1 against realised quality labels.

    When ``side`` is supplied, grade that side as a binary class. Otherwise grade
    CALL and PUT fires together as directional predictions.
    """
    predicted = signal.reindex(actual_quality_label.index)
    actual = actual_quality_label.astype(object)
    if side is not None:
        fired = predicted.eq(side)
        opportunities = actual.eq(side)
        correct = fired & opportunities
    else:
        fired = predicted.isin([CALL, PUT])
        opportunities = actual.isin([CALL, PUT])
        correct = fired & predicted.eq(actual)
    n_fired = int(fired.sum())
    n_opportunities = int(opportunities.sum())
    n_correct = int(correct.sum())
    precision = n_correct / n_fired if n_fired else None
    recall = n_correct / n_opportunities if n_opportunities else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and (precision + recall) > 0
        else None
    )
    return {
        "qualityBased_correct": n_correct,
        "qualityBased_precision": precision,
        "qualityBased_recall": recall,
        "qualityBased_F1": f1,
    }


def summarize_signal_quality(signal: pd.Series, outcomes: pd.DataFrame) -> dict[str, float | int | None]:
    """Aggregate quality over scorable CALL/PUT fires only."""
    quality = signal_quality(signal, outcomes["raw_signal_quality"])
    fired = signal.isin([CALL, PUT]).reindex(outcomes.index, fill_value=False)
    scored = quality[fired & quality.notna()]
    return {
        "quality_scored_fires": int(scored.size),
        "mean_signal_quality": round(float(scored.mean()), 6) if not scored.empty else None,
        "median_signal_quality": round(float(scored.median()), 6) if not scored.empty else None,
        "positive_quality_rate_pct": round(float(scored.gt(0).mean() * 100), 1) if not scored.empty else None,
    }
