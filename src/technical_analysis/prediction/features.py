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
    "adx14",
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
    "price_zscore_20d",
    "obv",
    "obv_slope_10d",
    "swing_structure_state",
    "rsi_divergence_flag",
    "candle_pattern_flag",
    "pattern_at_key_level",
    "volume_10d",
    "volume_20d",
    "volume_hybrid",
    "trend_efficiency_5d",
    "trend_efficiency_10d",
    "trend_efficiency_20d",
    "trend_efficiency_60d",
    "relative_strength_vs_sector",
    "breadth_advance_decline_ratio",
    "breadth_new_high_ratio",
    "banknifty_rel_strength",
    "banknifty_rel_strength_5d",
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
    "resistance_distance_40d",
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


def _wilder_smooth_sum(values: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing of raw values as recursive sums.

    First defined value is the simple sum of the first `period` points at index
    `period - 1`; subsequent values follow Wilder's recursive update.
    """
    out = pd.Series(index=values.index, dtype=float)
    if len(values) < period:
        return out
    initial = float(values.iloc[:period].sum())
    out.iloc[period - 1] = initial
    for i in range(period, len(values)):
        prev = out.iloc[i - 1]
        out.iloc[i] = prev - (prev / period) + float(values.iloc[i])
    return out


def compute_adx(window: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ADX using classic Wilder smoothing.

    Steps:
    1) raw +DM/-DM and TR
    2) Wilder-smoothed sums of +DM/-DM/TR
    3) +DI/-DI and DX
    4) ADX as Wilder-smoothed DX, first value at index 2*period-2
    """
    highs = get_column(window, "high_price")
    lows = get_column(window, "low_price")
    if highs is None or lows is None:
        return pd.Series(dtype=float)

    up_move = highs.diff()
    down_move = -lows.diff()  # prev_low - low

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = compute_true_range(window).fillna(0.0)
    smoothed_tr = _wilder_smooth_sum(tr, period)
    smoothed_plus_dm = _wilder_smooth_sum(plus_dm.fillna(0.0), period)
    smoothed_minus_dm = _wilder_smooth_sum(minus_dm.fillna(0.0), period)

    denom_tr = smoothed_tr.replace(0.0, pd.NA)
    plus_di = 100.0 * (smoothed_plus_dm / denom_tr)
    minus_di = 100.0 * (smoothed_minus_dm / denom_tr)

    di_sum = (plus_di + minus_di).replace(0.0, pd.NA)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum

    adx = pd.Series(index=dx.index, dtype=float)
    first_adx_idx = 2 * period - 1
    if len(dx) <= first_adx_idx:
        return adx

    seed = dx.iloc[period : 2 * period]
    if seed.notna().all():
        adx.iloc[first_adx_idx] = float(seed.mean())
    else:
        return adx

    for i in range(first_adx_idx + 1, len(dx)):
        prev = adx.iloc[i - 1]
        curr_dx = dx.iloc[i]
        if pd.isna(prev) or pd.isna(curr_dx):
            continue
        adx.iloc[i] = (float(prev) * (period - 1) + float(curr_dx)) / period

    return adx


def _fractal_swing_high_indices(series: pd.Series, left: int = 2, right: int = 2) -> list[int]:
    values = series.astype(float)
    n = len(values)
    out: list[int] = []
    if n < left + right + 1:
        return out
    max_confirmed = n - 1 - right
    for i in range(left, max_confirmed + 1):
        center = float(values.iloc[i])
        prev = values.iloc[i - left : i]
        nxt = values.iloc[i + 1 : i + 1 + right]
        if prev.empty or nxt.empty:
            continue
        if center > float(prev.max()) and center > float(nxt.max()):
            out.append(i)
    return out


def _fractal_swing_low_indices(series: pd.Series, left: int = 2, right: int = 2) -> list[int]:
    values = series.astype(float)
    n = len(values)
    out: list[int] = []
    if n < left + right + 1:
        return out
    max_confirmed = n - 1 - right
    for i in range(left, max_confirmed + 1):
        center = float(values.iloc[i])
        prev = values.iloc[i - left : i]
        nxt = values.iloc[i + 1 : i + 1 + right]
        if prev.empty or nxt.empty:
            continue
        if center < float(prev.min()) and center < float(nxt.min()):
            out.append(i)
    return out


def compute_swing_structure_state(
    highs: pd.Series,
    lows: pd.Series,
    close: float | None,
) -> str:
    """Compute swing structure from confirmed 2-left/2-right fractal pivots."""
    if close is None:
        return "mixed"

    swing_high_idx = _fractal_swing_high_indices(highs, left=2, right=2)
    swing_low_idx = _fractal_swing_low_indices(lows, left=2, right=2)
    if len(swing_high_idx) < 2 or len(swing_low_idx) < 2:
        return "mixed"

    h1 = float(highs.iloc[swing_high_idx[-2]])
    h2 = float(highs.iloc[swing_high_idx[-1]])
    l1 = float(lows.iloc[swing_low_idx[-2]])
    l2 = float(lows.iloc[swing_low_idx[-1]])

    uptrend_intact = (h2 > h1) and (l2 > l1)
    downtrend_intact = (h2 < h1) and (l2 < l1)

    if uptrend_intact:
        return "uptrend_intact"
    if downtrend_intact:
        return "downtrend_intact"

    # Structure break uses latest confirmed swing levels and prior trend context.
    # If the last confirmed structure was downtrend and close breaks above last SH,
    # mark structure_broken_up; symmetric for prior uptrend.
    if close > h2 and (h2 < h1 or l2 < l1):
        return "structure_broken_up"
    if close < l2 and (h2 > h1 or l2 > l1):
        return "structure_broken_down"

    # Consolidation: compressing swing structure (lower highs + higher lows)
    # or a near-flat ceiling and floor relative to current NIFTY price.
    nifty_threshold = abs(close) * 0.002
    is_flat_ceiling = abs(h2 - h1) <= nifty_threshold
    is_flat_floor = abs(l2 - l1) <= nifty_threshold
    if ((h2 < h1) and (l2 > l1)) or (is_flat_ceiling and is_flat_floor):
        return "consolidation"

    return "mixed"


def compute_rsi_divergence_flag(
    closes: pd.Series,
    rsi14: pd.Series,
    carry_forward: bool = True,
) -> str | None:
    """Detect RSI divergence on confirmed swing-event days.

    A swing at pivot index `i` is only confirmed on day `i+2` (2-right fractal).
    The divergence flag is evaluated only on those confirmation days. On non-event
    days, callers may carry forward the last evaluated flag or keep NULL.
    """
    swing_high_idx = _fractal_swing_high_indices(closes, left=2, right=2)
    swing_low_idx = _fractal_swing_low_indices(closes, left=2, right=2)

    n = len(closes)
    event_map: dict[int, list[tuple[str, int]]] = {}
    for idx in swing_high_idx:
        day = idx + 2
        if day < n:
            event_map.setdefault(day, []).append(("high", idx))
    for idx in swing_low_idx:
        day = idx + 2
        if day < n:
            event_map.setdefault(day, []).append(("low", idx))

    if not event_map:
        return None

    confirmed_highs: list[tuple[float, float]] = []
    confirmed_lows: list[tuple[float, float]] = []
    day_flag: dict[int, str] = {}

    for day in sorted(event_map):
        flag = "none"
        for kind, idx in event_map[day]:
            r = rsi14.iloc[idx]
            if pd.isna(r):
                continue
            price = float(closes.iloc[idx])
            rsi_val = float(r)

            if kind == "high":
                confirmed_highs.append((price, rsi_val))
                if len(confirmed_highs) >= 2:
                    p_prev, r_prev = confirmed_highs[-2]
                    p_curr, r_curr = confirmed_highs[-1]
                    if p_curr > p_prev and r_curr < r_prev:
                        flag = "bearish"
            else:
                confirmed_lows.append((price, rsi_val))
                if len(confirmed_lows) >= 2 and flag != "bearish":
                    p_prev, r_prev = confirmed_lows[-2]
                    p_curr, r_curr = confirmed_lows[-1]
                    if p_curr < p_prev and r_curr > r_prev:
                        flag = "bullish"

        day_flag[day] = flag

    current_day = n - 1
    if current_day in day_flag:
        return day_flag[current_day]
    if not carry_forward:
        return None

    past_days = [d for d in day_flag if d <= current_day]
    if not past_days:
        return None
    return day_flag[max(past_days)]


def compute_candle_pattern_flag(
    opens: pd.Series,
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
) -> str:
    """Classify the latest session candle using the requested precedence."""
    if len(opens) == 0 or len(highs) == 0 or len(lows) == 0 or len(closes) == 0:
        return "none"

    o = float(opens.iloc[-1])
    h = float(highs.iloc[-1])
    l = float(lows.iloc[-1])
    c = float(closes.iloc[-1])

    body = abs(c - o)
    range_full = h - l
    if range_full <= 0:
        return "none"

    upper_wick = h - max(c, o)
    lower_wick = min(c, o) - l
    body_ratio = body / range_full

    is_bullish_engulf = False
    is_bearish_engulf = False
    if len(opens) >= 2 and len(closes) >= 2:
        prev_o = float(opens.iloc[-2])
        prev_c = float(closes.iloc[-2])
        is_bullish_engulf = (prev_c < prev_o) and (c > o) and (o < prev_c) and (c > prev_o)
        is_bearish_engulf = (prev_c > prev_o) and (c < o) and (o > prev_c) and (c < prev_o)

    if is_bullish_engulf:
        return "bullish_engulf"
    if is_bearish_engulf:
        return "bearish_engulf"

    is_hammer = (lower_wick >= 2.0 * body) and (upper_wick <= 0.3 * body) and (body_ratio < 0.35)
    is_shooting_star = (upper_wick >= 2.0 * body) and (lower_wick <= 0.3 * body) and (body_ratio < 0.35)
    is_doji = body_ratio < 0.1

    if is_hammer:
        return "hammer"
    if is_shooting_star:
        return "shooting_star"
    if is_doji:
        return "doji"
    return "none"


def compute_pattern_at_key_level(
    candle_pattern_flag: str,
    near_validated_support_10d: bool | None,
    near_validated_resistance_10d: bool | None,
) -> str:
    if near_validated_support_10d or near_validated_resistance_10d:
        return candle_pattern_flag
    return "none"


def compute_obv(closes: pd.Series, volumes: pd.Series) -> pd.Series:
    """Compute classic On-Balance Volume series from closes and daily volume."""
    close_values = closes.astype(float)
    vol_values = volumes.astype(float).fillna(0.0)
    out = pd.Series(0.0, index=close_values.index, dtype=float)
    if len(close_values) <= 1:
        return out

    for i in range(1, len(close_values)):
        prev_obv = float(out.iloc[i - 1])
        c_prev = close_values.iloc[i - 1]
        c_curr = close_values.iloc[i]
        v_curr = float(vol_values.iloc[i])
        if pd.isna(c_prev) or pd.isna(c_curr):
            out.iloc[i] = prev_obv
        elif c_curr > c_prev:
            out.iloc[i] = prev_obv + v_curr
        elif c_curr < c_prev:
            out.iloc[i] = prev_obv - v_curr
        else:
            out.iloc[i] = prev_obv
    return out


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


def compute_banknifty_relative_strength(
    nifty_window: PredictionInput,
    banknifty_window: PredictionInput | None,
) -> tuple[float | None, float | None]:
    if banknifty_window is None:
        return None, None

    nifty_closes = get_closes(nifty_window)
    bank_closes = get_closes(banknifty_window)
    merged = pd.concat(
        [
            nifty_closes.rename("nifty_close"),
            bank_closes.rename("banknifty_close"),
        ],
        axis=1,
    ).dropna()
    if len(merged) < 2:
        return None, None

    nifty_ret = merged["nifty_close"].pct_change()
    bank_ret = merged["banknifty_close"].pct_change()
    rel = (bank_ret - nifty_ret).dropna()
    if rel.empty:
        return None, None

    rel_1d = float(rel.iloc[-1])
    rel_5d = float(rel.tail(5).mean()) if len(rel) >= 5 else None
    return rel_1d, rel_5d


def compute_underlying_features(
    window: PredictionInput,
    sector_window: PredictionInput | None = None,
    banknifty_window: PredictionInput | None = None,
    breadth_advance_decline_ratio: float | None = None,
    breadth_new_high_ratio: float | None = None,
) -> FeatureOutput:
    closes = get_closes(window)
    highs = get_column(window, "high_price")
    lows = get_column(window, "low_price")
    opens = get_column(window, "open_price")
    volumes = get_column(window, "volume")
    current_close = float(closes.iloc[-1]) if len(closes) else None

    rsi14_series = compute_rsi(closes, 14)
    features: FeatureOutput = {
        "ma10": round_feature(closes.tail(10).mean()) if len(closes) >= 10 else None,
        "ma20": round_feature(closes.tail(20).mean()) if len(closes) >= 20 else None,
        "ma50": round_feature(closes.tail(50).mean()) if len(closes) >= 50 else None,
        "ma90": round_feature(closes.tail(90).mean()) if len(closes) >= 90 else None,
        "rsi14": round_feature(rsi14_series.iloc[-1]) if len(closes) >= 16 else None,
        "rsi5": round_feature(compute_rsi(closes, 5).iloc[-1]) if len(closes) >= 7 else None,
        "adx14": None,
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
        "price_zscore_20d": None,
        "obv": None,
        "obv_slope_10d": None,
        "swing_structure_state": "mixed",
        "rsi_divergence_flag": None,
        "candle_pattern_flag": "none",
        "pattern_at_key_level": "none",
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
        "breadth_advance_decline_ratio": round_feature(breadth_advance_decline_ratio, 6),
        "breadth_new_high_ratio": round_feature(breadth_new_high_ratio, 6),
        "banknifty_rel_strength": None,
        "banknifty_rel_strength_5d": None,
        "ma5d_slope": round_feature(_ma_slope(closes, 5), 6),
        "ma10d_slope": round_feature(_ma_slope(closes, 10), 6),
        "ma20_slope": round_feature(_ma_slope(closes, 20), 6),
        "ma50_slope": round_feature(_ma_slope(closes, 50), 6),
    }

    if len(closes) >= 20:
        close_20 = closes.tail(20)
        mean_20 = float(close_20.mean())
        std_20 = float(close_20.std(ddof=0))
        if std_20 > 0:
            features["price_zscore_20d"] = round_feature((current_close - mean_20) / std_20, 6)

    if highs is not None and lows is not None and len(closes) >= 7:
        features["swing_structure_state"] = compute_swing_structure_state(highs, lows, current_close)
    if len(closes) >= 7:
        features["rsi_divergence_flag"] = compute_rsi_divergence_flag(
            closes,
            rsi14_series,
            carry_forward=True,
        )

    bank_rel_1d, bank_rel_5d = compute_banknifty_relative_strength(window, banknifty_window)
    features["banknifty_rel_strength"] = round_feature(bank_rel_1d, 6)
    features["banknifty_rel_strength_5d"] = round_feature(bank_rel_5d, 6)

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
        adx14 = compute_adx(window, 14)
        features["adx14"] = round_feature(adx14.iloc[-1], 6) if len(adx14) >= 28 else None
    else:
        features["atr7"] = None
        features["atr7_sma"] = None
        features["atr14"] = None
        features["atr14_sma"] = None
        features["adx14"] = None

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
    recent_high_40d = None
    if highs is not None and len(closes) >= 2:
        prior_highs_40d = highs.iloc[:-1].tail(40)
        if not prior_highs_40d.empty:
            recent_high_40d = round_feature(prior_highs_40d.max())
    features["resistance_distance_40d"] = round_feature(
        (float(recent_high_40d) - current_close) / current_close,
        6,
    ) if recent_high_40d is not None and current_close not in (None, 0) else None
    atr14_val = features.get("atr14")
    features["support_distance_10d"] = round_feature(
        (current_close - float(support_level_10d)) / float(atr14_val),
        6,
    ) if support_level_10d is not None and atr14_val not in (None, 0) and current_close is not None else None

    _add_validated_support_resistance_features(
        features=features,
        closes=closes,
        highs=highs,
        lows=lows,
        current_close=current_close,
        support_level=support_level_10d,
        resistance_level=resistance_level_10d,
    )

    if opens is not None and highs is not None and lows is not None and len(closes) >= 1:
        features["candle_pattern_flag"] = compute_candle_pattern_flag(opens, highs, lows, closes)

    if volumes is not None and len(closes) >= 1:
        obv_series = compute_obv(closes, volumes)
        features["obv"] = round_feature(obv_series.iloc[-1], 4)
        if len(obv_series) >= 11:
            features["obv_slope_10d"] = round_feature(
                (float(obv_series.iloc[-1]) - float(obv_series.iloc[-11])) / 10.0,
                4,
            )

    features["pattern_at_key_level"] = compute_pattern_at_key_level(
        features.get("candle_pattern_flag") or "none",
        features.get("near_validated_support_10d"),
        features.get("near_validated_resistance_10d"),
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
