"""Point-in-time macro feature engineering for NIFTY signal dates."""
from __future__ import annotations

import numpy as np
import pandas as pd


MACRO_FEATURE_COLUMNS = [
    "crude_ret_1d",
    "crude_ret_5d",
    "us10y_chg_1d",
    "us10y_chg_5d",
    "us10y_level",
    "usdinr_ret_1d",
    "usdinr_ret_5d",
    "fii_net_cr",
    "fii_net_1d_cr",
    "fii_net_5d_cr",
    "dii_net_cr",
    "fii_idx_long_ratio",
    "fii_idx_long_ratio_chg5",
    "crude_ret_5d_z",
    "us10y_chg_5d_z",
    "usdinr_ret_5d_z",
    "fii_net_5d_z",
    "fii_idx_5d_z",
    "macro_risk_off",
]


def _rolling_zscore(values: pd.Series, window: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.dropna()
    mean = valid.rolling(window=window, min_periods=window).mean()
    std = valid.rolling(window=window, min_periods=window).std(ddof=0)
    return ((valid - mean) / std.replace(0, np.nan)).reindex(numeric.index)


def _shifted_rolling_robust_zscore(values: pd.Series, window: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.dropna()
    history = valid.shift(1)
    median = history.rolling(window=window, min_periods=window).median()
    mad = history.rolling(window=window, min_periods=window).apply(
        lambda sample: np.median(np.abs(sample - np.median(sample))),
        raw=True,
    )
    zscore = (valid - median) / (1.4826 * mad.replace(0, np.nan))
    return zscore.clip(lower=-3.0, upper=3.0).reindex(numeric.index)


def _source_pct_change(values: pd.Series, periods: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.dropna().pct_change(periods, fill_method=None).reindex(numeric.index)


def _source_diff(values: pd.Series, periods: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.dropna().diff(periods).reindex(numeric.index)


def _source_rolling_sum(values: pd.Series, window: int) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return (
        numeric.dropna()
        .rolling(window=window, min_periods=window)
        .sum()
        .reindex(numeric.index)
    )


def calculate_macro_features(raw: pd.DataFrame, trailing_window: int) -> pd.DataFrame:
    if trailing_window < 2:
        raise ValueError("trailing_window must be >= 2")
    if raw.empty:
        return pd.DataFrame(columns=["signal_date", *MACRO_FEATURE_COLUMNS])

    required = {
        "factor_date",
        "crude_close",
        "us10y_level",
        "usdinr_close",
        "fii_net_cr",
        "dii_net_cr",
        "fii_idx_fut_long",
        "fii_idx_fut_short",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"Missing macro input columns: {', '.join(missing)}")

    frame = raw.copy()
    frame["signal_date"] = pd.to_datetime(frame["factor_date"], errors="coerce")
    frame = frame.dropna(subset=["signal_date"]).sort_values("signal_date").reset_index(drop=True)

    crude = pd.to_numeric(frame["crude_close"], errors="coerce")
    us10y = pd.to_numeric(frame["us10y_level"], errors="coerce")
    usdinr = pd.to_numeric(frame["usdinr_close"], errors="coerce")
    fii = pd.to_numeric(frame["fii_net_cr"], errors="coerce")

    frame["crude_ret_1d"] = _source_pct_change(crude, 1)
    frame["crude_ret_5d"] = _source_pct_change(crude, 5)
    frame["us10y_chg_1d"] = _source_diff(us10y, 1) * 100.0
    frame["us10y_chg_5d"] = _source_diff(us10y, 5) * 100.0
    frame["usdinr_ret_1d"] = _source_pct_change(usdinr, 1)
    frame["usdinr_ret_5d"] = _source_pct_change(usdinr, 5)
    frame["fii_net_cr"] = fii
    frame["fii_net_1d_cr"] = fii
    frame["fii_net_5d_cr"] = _source_rolling_sum(fii, 5)
    frame["dii_net_cr"] = pd.to_numeric(frame["dii_net_cr"], errors="coerce")
    fii_idx_long = pd.to_numeric(frame["fii_idx_fut_long"], errors="coerce")
    fii_idx_short = pd.to_numeric(frame["fii_idx_fut_short"], errors="coerce")
    fii_idx_total = fii_idx_long + fii_idx_short
    frame["fii_idx_long_ratio"] = (fii_idx_long / fii_idx_total).where(
        fii_idx_total > 0
    )
    frame["fii_idx_long_ratio_chg5"] = _source_diff(
        frame["fii_idx_long_ratio"], 5
    )

    components = {
        "crude_ret_5d_z": _shifted_rolling_robust_zscore(
            frame["crude_ret_5d"],
            trailing_window,
        ),
        "us10y_chg_5d_z": _rolling_zscore(frame["us10y_chg_5d"], trailing_window),
        "usdinr_ret_5d_z": _shifted_rolling_robust_zscore(
            frame["usdinr_ret_5d"],
            trailing_window,
        ),
        "fii_net_5d_z": _rolling_zscore(frame["fii_net_5d_cr"], trailing_window),
        "fii_idx_5d_z": -_shifted_rolling_robust_zscore(
            frame["fii_idx_long_ratio_chg5"],
            trailing_window,
        ),
    }
    for column, values in components.items():
        frame[column] = values

    weighted = pd.DataFrame(
        {
            "crude": frame["crude_ret_5d_z"] * 0.25,
            "us10y": frame["us10y_chg_5d_z"] * 0.50,
            "usdinr": frame["usdinr_ret_5d_z"] * 0.25,
        },
        index=frame.index,
    )
    available_weights = pd.DataFrame(
        {
            "crude": frame["crude_ret_5d_z"].notna() * 0.25,
            "us10y": frame["us10y_chg_5d_z"].notna() * 0.50,
            "usdinr": frame["usdinr_ret_5d_z"].notna() * 0.25,
        },
        index=frame.index,
    )
    weight_sum = available_weights.sum(axis=1)
    frame["macro_risk_off"] = weighted.sum(axis=1, skipna=True) / weight_sum
    frame.loc[weight_sum == 0, "macro_risk_off"] = np.nan

    frame["signal_date"] = frame["signal_date"].dt.date
    return frame[["signal_date", *MACRO_FEATURE_COLUMNS]]
