from __future__ import annotations


from datetime import date, datetime
from typing import Any

import pandas as pd

from .features import compute_underlying_features
from .schema import Regime, RegimeSnapshot, UnderlyingFeatureSnapshot


def as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_regime(value: object) -> Regime:
    regime = str(value or "UNKNOWN").upper()
    if regime in {"TREND_UP", "TREND_DOWN", "RANGE", "CHOPPY", "UNKNOWN"}:
        return regime  # type: ignore[return-value]
    return "UNKNOWN"


def build_feature_snapshot(
    symbol: str,
    trade_date: str | date | datetime,
    window: pd.Series | pd.DataFrame,
    sector_window: pd.Series | pd.DataFrame | None = None,
    benchmark_window: pd.Series | pd.DataFrame | None = None,
) -> UnderlyingFeatureSnapshot:
    features = compute_underlying_features(window, sector_window=sector_window)
    close = None
    volume = None
    volume_avg_20d = None
    if isinstance(window, pd.DataFrame) and not window.empty:
        close = as_float(window.iloc[-1].get("close_price"))
        volume = as_float(window.iloc[-1].get("volume"))
        if "volume" in window.columns and len(window) >= 20:
            volume_avg_20d = as_float(window["volume"].astype(float).tail(20).mean())
    elif isinstance(window, pd.Series) and not window.empty:
        close = as_float(window.iloc[-1])

    relative_strength_vs_benchmark = None
    if benchmark_window is not None:
        from .features import compute_relative_strength_vs_sector

        relative_strength_vs_benchmark = as_float(
            compute_relative_strength_vs_sector(window, benchmark_window, 20)
        )

    return UnderlyingFeatureSnapshot(
        symbol=symbol.upper(),
        trade_date=_date_str(trade_date),
        close=close,
        volume=volume,
        ma10=as_float(features.get("ma10")),
        ma20=as_float(features.get("ma20")),
        ma50=as_float(features.get("ma50")),
        ma90=as_float(features.get("ma90")),
        ma20_slope=as_float(features.get("ma20_slope")),
        ma50_slope=as_float(features.get("ma50_slope")),
        rsi14=as_float(features.get("rsi14")),
        atr14=as_float(features.get("atr14")),
        bb_upper=as_float(features.get("bb_upper")),
        bb_middle=as_float(features.get("bb_middle")),
        bb_lower=as_float(features.get("bb_lower")),
        bb_width=as_float(features.get("bb_width")),
        ret_5d=as_float(features.get("ret_5d")),
        ret_20d=as_float(features.get("ret_20d")),
        ret_60d=as_float(features.get("ret_60d")),
        volatility_20d=as_float(features.get("volatility_20d")),
        volume_avg_20d=volume_avg_20d,
        volume_ratio=None,
        trend_efficiency=as_float(features.get("trend_efficiency_60d")),
        range_position=as_float(features.get("range_position_20d")),
        relative_strength_vs_sector=as_float(features.get("relative_strength_vs_sector")),
        relative_strength_vs_benchmark=relative_strength_vs_benchmark,
    )


def build_regime_snapshot(
    stock_regime: str,
    sector_regime: str | None = None,
    benchmark_regime: str | None = None,
    regime_reasons: list[str] | None = None,
) -> RegimeSnapshot:
    return RegimeSnapshot(
        stock_regime=normalize_regime(stock_regime),
        sector_regime=normalize_regime(sector_regime) if sector_regime is not None else None,
        benchmark_regime=normalize_regime(benchmark_regime) if benchmark_regime is not None else None,
        stock_regime_confidence=None,
        sector_regime_confidence=None,
        benchmark_regime_confidence=None,
        regime_reasons=regime_reasons or [f"Stock regime detected as {normalize_regime(stock_regime)}"],
    )


def _date_str(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
