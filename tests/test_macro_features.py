from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.common.config import (
    get_macro_trailing_window,
    get_nifty_target_pct_relaxed,
)
from src.data_manager.macro_factor_loader import (
    _none_if_missing,
    normalize_fii_dii_records,
    normalize_participant_oi_csv,
    participant_oi_archive_url,
)
from src.technical_analysis.cascade.constants import CALL, FLAT, PUT
from src.technical_analysis.cascade.strategies import macro_direction
from src.technical_analysis.cascade.engine import score_directional_label
from src.technical_analysis.macro_features import calculate_macro_features


def _raw_macro_frame(rows: int = 70) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "factor_date": [date(2026, 1, 1) + timedelta(days=i) for i in range(rows)],
            "crude_close": np.linspace(70.0, 90.0, rows),
            "us10y_level": np.linspace(3.8, 4.6, rows),
            "usdinr_close": np.linspace(83.0, 87.0, rows),
            "fii_net_cr": np.linspace(500.0, -800.0, rows),
            "dii_net_cr": np.linspace(100.0, 400.0, rows),
            "fii_idx_fut_long": np.linspace(100_000, 200_000, rows),
            "fii_idx_fut_short": np.linspace(300_000, 150_000, rows),
        }
    )


def test_macro_features_use_five_sessions_and_renormalize_missing_component() -> None:
    raw = _raw_macro_frame()
    raw.loc[len(raw) - 1, "fii_net_cr"] = np.nan

    result = calculate_macro_features(raw, trailing_window=20)
    latest = result.iloc[-1]

    expected_crude = raw.iloc[-1]["crude_close"] / raw.iloc[-6]["crude_close"] - 1
    expected_yield_bp = (
        raw.iloc[-1]["us10y_level"] - raw.iloc[-6]["us10y_level"]
    ) * 100
    assert latest["crude_ret_5d"] == pytest.approx(expected_crude)
    assert latest["us10y_chg_5d"] == pytest.approx(expected_yield_bp)
    assert pd.isna(latest["fii_net_1d_cr"])
    assert pd.isna(latest["fii_net_cr"])
    assert pd.isna(latest["fii_net_5d_cr"])
    expected_composite = (
        0.25 * latest["crude_ret_5d_z"]
        + 0.50 * latest["us10y_chg_5d_z"]
        + 0.25 * latest["usdinr_ret_5d_z"]
    )
    assert latest["macro_risk_off"] == pytest.approx(expected_composite)


def test_macro_windows_use_each_sources_non_missing_history() -> None:
    raw = _raw_macro_frame(35)
    raw.loc[[3, 8, 13], "crude_close"] = np.nan
    raw.loc[[5, 11], "us10y_level"] = np.nan

    result = calculate_macro_features(raw, trailing_window=20)
    latest = result.iloc[-1]
    crude_valid = raw["crude_close"].dropna()
    yield_valid = raw["us10y_level"].dropna()

    assert latest["crude_ret_5d"] == pytest.approx(
        crude_valid.iloc[-1] / crude_valid.iloc[-6] - 1
    )
    assert latest["us10y_chg_5d"] == pytest.approx(
        (yield_valid.iloc[-1] - yield_valid.iloc[-6]) * 100
    )
    assert pd.notna(latest["crude_ret_5d_z"])
    assert pd.notna(latest["us10y_chg_5d_z"])


def test_participant_oi_features_use_fii_index_futures_and_five_sessions() -> None:
    raw = _raw_macro_frame(10)
    raw.loc[4, ["fii_idx_fut_long", "fii_idx_fut_short"]] = np.nan

    result = calculate_macro_features(raw, trailing_window=3)
    latest = result.iloc[-1]
    ratios = (
        raw["fii_idx_fut_long"]
        / (raw["fii_idx_fut_long"] + raw["fii_idx_fut_short"])
    ).dropna()

    assert latest["fii_idx_long_ratio"] == pytest.approx(ratios.iloc[-1])
    assert latest["fii_idx_long_ratio_chg5"] == pytest.approx(
        ratios.iloc[-1] - ratios.iloc[-6]
    )


def test_fii_idx_robust_z_uses_shifted_history_clips_and_flips_sign() -> None:
    raw = _raw_macro_frame(140)
    ratios = 0.25 + np.sin(np.arange(len(raw)) / 6.0) * 0.04
    totals = np.full(len(raw), 500_000)
    raw["fii_idx_fut_long"] = ratios * totals
    raw["fii_idx_fut_short"] = (1 - ratios) * totals
    result = calculate_macro_features(raw, trailing_window=120)

    changes = pd.Series(ratios).diff(5).dropna()
    history = changes.iloc[-121:-1]
    center = history.median()
    mad = np.median(np.abs(history - center))
    expected = -np.clip(
        (changes.iloc[-1] - center) / (1.4826 * mad),
        -3,
        3,
    )

    assert result.iloc[-1]["fii_idx_5d_z"] == pytest.approx(expected)
    assert result.iloc[124]["fii_idx_5d_z"] != result.iloc[124]["fii_idx_5d_z"]
    assert -3 <= result.iloc[-1]["fii_idx_5d_z"] <= 3


def test_crude_and_usdinr_use_shifted_robust_clipped_zscores() -> None:
    raw = _raw_macro_frame(140)
    raw.loc[len(raw) - 1, "crude_close"] *= 3
    raw.loc[len(raw) - 1, "usdinr_close"] *= 0.5

    result = calculate_macro_features(raw, trailing_window=120)
    latest = result.iloc[-1]

    assert latest["crude_ret_5d_z"] == pytest.approx(3.0)
    assert latest["usdinr_ret_5d_z"] == pytest.approx(-3.0)


def test_macro_composite_ignores_fii_cash_flow_zscore() -> None:
    raw = _raw_macro_frame(140)
    baseline = calculate_macro_features(raw, trailing_window=120)
    raw["fii_net_cr"] = np.linspace(-1_000_000, 1_000_000, len(raw)) ** 3
    changed = calculate_macro_features(raw, trailing_window=120)

    assert baseline.iloc[-1]["fii_net_5d_z"] != pytest.approx(
        changed.iloc[-1]["fii_net_5d_z"]
    )
    assert baseline.iloc[-1]["macro_risk_off"] == pytest.approx(
        changed.iloc[-1]["macro_risk_off"]
    )


def test_macro_composite_ignores_fii_positioning_zscore() -> None:
    raw = _raw_macro_frame(140)
    baseline = calculate_macro_features(raw, trailing_window=120)
    raw.loc[len(raw) - 1, "fii_idx_fut_long"] = 1
    raw.loc[len(raw) - 1, "fii_idx_fut_short"] = 1_000_000
    changed = calculate_macro_features(raw, trailing_window=120)

    assert baseline.iloc[-1]["fii_idx_5d_z"] != pytest.approx(
        changed.iloc[-1]["fii_idx_5d_z"]
    )
    assert baseline.iloc[-1]["macro_risk_off"] == pytest.approx(
        changed.iloc[-1]["macro_risk_off"]
    )


def test_macro_composite_renormalizes_fixed_weights_when_input_missing() -> None:
    raw = _raw_macro_frame(140)
    raw.loc[len(raw) - 1, "crude_close"] = np.nan

    latest = calculate_macro_features(raw, trailing_window=120).iloc[-1]
    expected = (
        0.50 * latest["us10y_chg_5d_z"]
        + 0.25 * latest["usdinr_ret_5d_z"]
    ) / 0.75

    assert latest["macro_risk_off"] == pytest.approx(expected)


def test_macro_direction_applies_entry_and_half_threshold_exit_bands() -> None:
    scores = [0.80, 0.60, 0.36, -0.80, -0.50, -0.36, 0.80, np.nan]
    signal = macro_direction(pd.DataFrame({"macro_risk_off": scores}))[
        "strategy_MacroDirection_signal"
    ]
    assert signal.tolist() == [PUT, PUT, FLAT, CALL, CALL, FLAT, PUT, FLAT]


def test_macro_trailing_window_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("MACRO_TRAILING_WINDOW", "120")
    assert get_macro_trailing_window() == 120
    monkeypatch.setenv("MACRO_TRAILING_WINDOW", "1")
    with pytest.raises(ValueError, match="must be >= 2"):
        get_macro_trailing_window()


def test_relaxed_target_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("NIFTY_TARGET_PCT_RELAXED", "0.004")
    assert get_nifty_target_pct_relaxed() == pytest.approx(0.004)


def test_relaxed_directional_metrics_use_relaxed_label() -> None:
    frame = pd.DataFrame(
        {
            "actual_trade_label_relaxed": ["CALL", "PUT", "BOTH", "NO_POSITION"],
        }
    )
    prediction = pd.Series(["CALL", "PUT", "CALL", "PUT"])

    metrics = score_directional_label(
        frame,
        prediction,
        "actual_trade_label_relaxed",
    )

    assert metrics["precision"] == pytest.approx(0.75)
    assert metrics["recall"] == pytest.approx(1.0)
    assert metrics["f1"] == pytest.approx(6 / 7)


def test_normalize_fii_dii_records_accepts_api_and_archive_columns() -> None:
    result = normalize_fii_dii_records(
        [
            {
                "date": "05-Oct-2026",
                "category": "FII/FPI",
                "buyValue": "12,000.50",
                "sellValue": "13,250.25",
            },
            {
                "Date": "05-Oct-2026",
                "Category": "DII",
                "Net Value (Rs. Cr)": "900.75",
            },
        ]
    )
    assert result.iloc[0]["fii_net_cr"] == pytest.approx(-1249.75)
    assert result.iloc[0]["dii_net_cr"] == pytest.approx(900.75)


def test_normalize_participant_oi_csv_extracts_fii_index_futures() -> None:
    content = """"Participant wise Open Interest (no. of contracts)",,,\nClient Type,Future Index Long,Future Index Short,Future Stock Long\nClient,301040,58614,3453975\nDII,49642,17592,245443\nFII,"29,394","312,264",3360170\nPro,35278,26884,826666\n"""
    factor_date = date(2026, 9, 30)

    result = normalize_participant_oi_csv(content, factor_date)

    assert result.to_dict("records") == [
        {
            "factor_date": factor_date,
            "fii_idx_fut_long": 29394,
            "fii_idx_fut_short": 312264,
        }
    ]
    assert participant_oi_archive_url(factor_date).endswith(
        "fao_participant_oi_30092026.csv"
    )


def test_missing_source_values_become_database_nulls() -> None:
    assert _none_if_missing(np.nan) is None
    assert _none_if_missing(pd.NA) is None
    assert _none_if_missing(4.2) == 4.2
