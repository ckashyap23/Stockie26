from __future__ import annotations

from typing import Any

import pandas as pd

PredictionInput = pd.Series | pd.DataFrame
FeatureOutput = dict[str, Any]
FEATURE_COLUMNS = [
    "ma10",
    "ma20",
    "ma50",
    "ma90",
    "rsi14",
    "rsi5",
    "atr7",
    "atr7_sma",
    "atr14",
    "atr14_sma",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_width",
    "ret_2d",
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ret_60d",
    "volatility_10d",
    "volatility_20d",
    "volume_10d",
    "volume_20d",
    "volume_hybrid",
    "trend_efficiency_5d",
    "trend_efficiency_10d",
    "trend_efficiency_20d",
    "trend_efficiency_60d",
    "relative_strength_vs_sector",
    "ma5d_slope",
    "ma10d_slope",
    "ma20_slope",
    "ma_slope_combo",
    "ma50_slope",
    "support_level_10d",
    "resistance_level_10d",
    "recent_high_5d",
    "recent_low_5d",
    "recent_high_10d",
    "resistance_distance_10d",
    "recent_low_10d",
    "support_distance_10d",
    "recent_high_20d",
    "recent_low_20d",
    "range_position_5d",
    "range_position_10d",
    "range_position_20d",
    "support_bounce_count_10d",
    "resistance_rejection_count_10d",
    "support_broken_10d",
    "resistance_broken_10d",
    "near_validated_support_10d",
    "near_validated_resistance_10d",
    "room_to_validated_resistance_10d",
]


def get_closes(window: PredictionInput) -> pd.Series:
    if isinstance(window, pd.Series):
        return window.astype(float)
    if not isinstance(window, pd.DataFrame):
        raise TypeError(f"window must be pd.Series or pd.DataFrame, got {type(window)}")
    if "close_price" not in window.columns:
        raise ValueError("DataFrame window must contain a 'close_price' column")
    return window["close_price"].astype(float)


def get_column(window: PredictionInput, col: str) -> pd.Series | None:
    if isinstance(window, pd.DataFrame) and col in window.columns:
        return window[col].astype(float)
    return None


def round_feature(value: Any, digits: int = 4) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    roll_down = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0.0, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def compute_true_range(window: pd.DataFrame) -> pd.Series:
    highs = get_column(window, "high_price")
    lows = get_column(window, "low_price")
    closes = get_closes(window)
    if highs is None or lows is None:
        return pd.Series(dtype=float)
    prev_close = closes.shift(1)
    ranges = pd.concat(
        [
            highs - lows,
            (highs - prev_close).abs(),
            (lows - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def compute_atr(window: pd.DataFrame, period: int = 14) -> pd.Series:
    true_range = compute_true_range(window)
    if true_range.empty:
        return true_range
    return true_range.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_atr_sma(window: pd.DataFrame, period: int = 14) -> pd.Series:
    """Simple moving average of true range, including the current bar."""
    true_range = compute_true_range(window)
    if true_range.empty:
        return true_range
    return true_range.rolling(window=period, min_periods=period).mean()


def compute_return(closes: pd.Series, days: int) -> float | None:
    if len(closes) < days + 1:
        return None
    start = float(closes.iloc[-(days + 1)])
    if start == 0:
        return None
    return float(closes.iloc[-1]) / start - 1.0


def compute_average_volume(window: PredictionInput, lookback: int) -> float | None:
    volumes = get_column(window, "volume")
    if volumes is None or len(volumes) < lookback:
        return None
    return float(volumes.tail(lookback).mean())


def compute_trend_efficiency(closes: pd.Series, days: int = 60) -> float | None:
    if len(closes) < days + 1:
        return None
    close_slice = closes.iloc[-(days + 1):]
    net_move = abs(float(close_slice.iloc[-1]) - float(close_slice.iloc[0]))
    path_move = float(close_slice.diff().abs().sum())
    if path_move == 0:
        return None
    return net_move / path_move


def compute_relative_strength_vs_sector(
    stock_window: PredictionInput,
    sector_window: PredictionInput | None,
    days: int = 20,
) -> float | None:
    if sector_window is None:
        return None
    stock_ret = compute_return(get_closes(stock_window), days)
    sector_ret = compute_return(get_closes(sector_window), days)
    if stock_ret is None or sector_ret is None:
        return None
    return stock_ret - sector_ret


def compute_underlying_features(
    window: PredictionInput,
    sector_window: PredictionInput | None = None,
) -> FeatureOutput:
    closes = get_closes(window)
    highs = get_column(window, "high_price")
    lows = get_column(window, "low_price")
    current_close = float(closes.iloc[-1]) if len(closes) else None

    features: FeatureOutput = {
        "ma10": round_feature(closes.tail(10).mean()) if len(closes) >= 10 else None,
        "ma20": round_feature(closes.tail(20).mean()) if len(closes) >= 20 else None,
        "ma50": round_feature(closes.tail(50).mean()) if len(closes) >= 50 else None,
        "ma90": round_feature(closes.tail(90).mean()) if len(closes) >= 90 else None,
        "rsi14": round_feature(compute_rsi(closes, 14).iloc[-1]) if len(closes) >= 16 else None,
        "rsi5": round_feature(compute_rsi(closes, 5).iloc[-1]) if len(closes) >= 7 else None,
        "ret_2d": round_feature(compute_return(closes, 2), 6),
        "ret_3d": round_feature(compute_return(closes, 3), 6),
        "ret_5d": round_feature(compute_return(closes, 5), 6),
        "ret_10d": round_feature(compute_return(closes, 10), 6),
        "ret_20d": round_feature(compute_return(closes, 20), 6),
        "ret_60d": round_feature(compute_return(closes, 60), 6),
        "volatility_10d": round_feature(closes.pct_change().dropna().tail(10).std(), 6)
        if len(closes) >= 11
        else None,
        "volatility_20d": round_feature(closes.pct_change().dropna().tail(20).std(), 6)
        if len(closes) >= 21
        else None,
        "volume_10d": round_feature(compute_average_volume(window, 10), 4),
        "volume_20d": round_feature(compute_average_volume(window, 20), 4),
        "trend_efficiency_5d": round_feature(compute_trend_efficiency(closes, 5), 6),
        "trend_efficiency_10d": round_feature(compute_trend_efficiency(closes, 10), 6),
        "trend_efficiency_20d": round_feature(compute_trend_efficiency(closes, 20), 6),
        "trend_efficiency_60d": round_feature(compute_trend_efficiency(closes, 60), 6),
        "relative_strength_vs_sector": round_feature(
            compute_relative_strength_vs_sector(window, sector_window, 20),
            6,
        ),
        "ma5d_slope": round_feature(_ma_slope(closes, 5), 6),
        "ma10d_slope": round_feature(_ma_slope(closes, 10), 6),
        "ma20_slope": round_feature(_ma_slope(closes, 20), 6),
        "ma50_slope": round_feature(_ma_slope(closes, 50), 6),
    }

    volumes = get_column(window, "volume")
    current_volume = float(volumes.iloc[-1]) if volumes is not None and len(volumes) else None
    volume_20d = features.get("volume_20d")
    features["volume_hybrid"] = round_feature(
        current_volume / float(volume_20d), 6
    ) if current_volume is not None and volume_20d not in (None, 0) else None
    slopes = [features.get("ma5d_slope"), features.get("ma10d_slope"), features.get("ma20_slope")]
    features["ma_slope_combo"] = round_feature(
        0.50 * float(slopes[0]) + 0.30 * float(slopes[1]) + 0.20 * float(slopes[2]),
        6,
    ) if all(value is not None for value in slopes) else None

    if isinstance(window, pd.DataFrame):
        atr7 = compute_atr(window, 7)
        features["atr7"] = round_feature(atr7.iloc[-1]) if len(atr7) >= 7 else None
        atr7_sma = compute_atr_sma(window, 7)
        features["atr7_sma"] = round_feature(atr7_sma.iloc[-1]) if len(atr7_sma) >= 7 else None
        atr14 = compute_atr(window, 14)
        features["atr14"] = round_feature(atr14.iloc[-1]) if len(atr14) >= 14 else None
        atr14_sma = compute_atr_sma(window, 14)
        features["atr14_sma"] = round_feature(atr14_sma.iloc[-1]) if len(atr14_sma) >= 14 else None
    else:
        features["atr7"] = None
        features["atr7_sma"] = None
        features["atr14"] = None
        features["atr14_sma"] = None

    if len(closes) >= 20:
        bb_middle = closes.rolling(20).mean().iloc[-1]
        bb_std = closes.rolling(20).std(ddof=0).iloc[-1]
        bb_upper = bb_middle + 2.0 * bb_std
        bb_lower = bb_middle - 2.0 * bb_std
        features["bb_upper"] = round_feature(bb_upper)
        features["bb_middle"] = round_feature(bb_middle)
        features["bb_lower"] = round_feature(bb_lower)
        features["bb_width"] = round_feature((bb_upper - bb_lower) / bb_middle, 6) if bb_middle else None
    else:
        features["bb_upper"] = None
        features["bb_middle"] = None
        features["bb_lower"] = None
        features["bb_width"] = None

    support_level_10d: float | None = None
    resistance_level_10d: float | None = None

    for lookback in (5, 10, 20):
        high_key = f"recent_high_{lookback}d"
        low_key = f"recent_low_{lookback}d"
        position_key = f"range_position_{lookback}d"
        if highs is not None and lows is not None and len(closes) >= 2:
            prior_highs = highs.iloc[:-1].tail(lookback)
            prior_lows = lows.iloc[:-1].tail(lookback)
            features[high_key] = round_feature(prior_highs.max()) if not prior_highs.empty else None
            features[low_key] = round_feature(prior_lows.min()) if not prior_lows.empty else None
            if lookback == 10:
                support_level_10d = round_feature(prior_lows.min()) if len(prior_lows) >= 10 else None
                resistance_level_10d = round_feature(prior_highs.max()) if len(prior_highs) >= 10 else None
            if current_close is not None and features[high_key] is not None and features[low_key] is not None:
                range_width = float(features[high_key]) - float(features[low_key])
                features[position_key] = round_feature(
                    (current_close - float(features[low_key])) / range_width,
                    6,
                ) if range_width else None
            else:
                features[position_key] = None
        else:
            features[high_key] = None
            features[low_key] = None
            features[position_key] = None

    features["support_level_10d"] = support_level_10d
    features["resistance_level_10d"] = resistance_level_10d
    recent_high_10d = resistance_level_10d
    features["resistance_distance_10d"] = round_feature(
        (float(recent_high_10d) - current_close) / current_close,
        6,
    ) if recent_high_10d is not None and current_close not in (None, 0) else None
    features["support_distance_10d"] = round_feature(
        (current_close - float(support_level_10d)) / current_close,
        6,
    ) if support_level_10d is not None and current_close not in (None, 0) else None

    _add_validated_support_resistance_features(
        features=features,
        closes=closes,
        highs=highs,
        lows=lows,
        current_close=current_close,
        support_level=support_level_10d,
        resistance_level=resistance_level_10d,
    )

    return {column: features.get(column) for column in FEATURE_COLUMNS}


def _ma_slope(closes: pd.Series, window: int, periods: int = 5) -> float | None:
    if len(closes) < window + periods:
        return None
    ma = closes.rolling(window).mean()
    previous = float(ma.iloc[-(periods + 1)])
    if previous == 0:
        return None
    return float(ma.iloc[-1]) / previous - 1.0


def _add_validated_support_resistance_features(
    features: FeatureOutput,
    closes: pd.Series,
    highs: pd.Series | None,
    lows: pd.Series | None,
    current_close: float | None,
    support_level: float | None,
    resistance_level: float | None,
) -> None:
    default_keys = {
        "support_bounce_count_10d": None,
        "resistance_rejection_count_10d": None,
        "support_broken_10d": None,
        "resistance_broken_10d": None,
        "near_validated_support_10d": None,
        "near_validated_resistance_10d": None,
        "room_to_validated_resistance_10d": None,
    }
    if (
        highs is None
        or lows is None
        or len(closes) < 2
        or current_close in (None, 0)
        or support_level is None
        or resistance_level is None
    ):
        features.update(default_keys)
        return

    prior_closes = closes.iloc[:-1].tail(10)
    prior_highs = highs.iloc[:-1].tail(10)
    prior_lows = lows.iloc[:-1].tail(10)
    support = float(support_level)
    resistance = float(resistance_level)
    close = float(current_close)

    support_bounces = (prior_lows <= support * 1.0025) & (prior_closes >= support * 1.001)
    resistance_rejections = (prior_highs >= resistance * 0.9975) & (prior_closes <= resistance * 0.999)
    # Broken when close crosses the level (no extra margin): this is the exact threshold
    # at which support_distance_10d / resistance_distance_10d flip sign, keeping the
    # two features consistent (distance < 0 ↔ broken = True).
    support_broken = close < support
    resistance_broken = close > resistance
    near_support = close <= support * 1.003 and int(support_bounces.sum()) >= 2 and not support_broken
    near_resistance = close >= resistance * 0.997 and int(resistance_rejections.sum()) >= 2 and not resistance_broken

    features["support_bounce_count_10d"] = int(support_bounces.sum())
    features["resistance_rejection_count_10d"] = int(resistance_rejections.sum())
    features["support_broken_10d"] = bool(support_broken)
    features["resistance_broken_10d"] = bool(resistance_broken)
    features["near_validated_support_10d"] = bool(near_support)
    features["near_validated_resistance_10d"] = bool(near_resistance)
    features["room_to_validated_resistance_10d"] = round_feature(
        (resistance - close) / close,
        6,
    ) if int(resistance_rejections.sum()) >= 2 and not resistance_broken else None
