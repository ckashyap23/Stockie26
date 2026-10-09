from __future__ import annotations

import pandas as pd

from src.technical_analysis.cascade.constants import CALL, FLAT, PUT
from src.technical_analysis.cascade.strategies import nifty_2026_call_put_research


def _base_row() -> dict:
    return {
        "rsi5": 28.0,
        "range_position_20d": 0.5,
        "price_zscore_20d": 0.4,
        "ma20": 100.0,
        "ma50": 99.0,
        "ma20_slope": 0.002,
        "ret_3d": -0.006,
        "atr14": 2.0,
        "ret_5d": 0.0,
        "vix_close": 14.0,
        "pcr_oi": 1.0,
        "open_915": 100.0,
        "high_day": 101.0,
        "low_day": 99.0,
        "close_1415": 101.5,
        "close_1515": 101.0,
        "oi_regime": "long_unwinding",
        "support_distance_10d": 0.2,
        "gap_open_atr": 0.0,
        "nifty_gap_pct": 0.001,
    }


def test_trend_pullback_call_fires() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 102.0,
            "ma50": 100.0,
            "ma20_slope": 0.003,
            "rsi5": 35.0,
            "vix_close": 13.5,
            "oi_regime": "long_unwinding",
            "gap_open_atr": 0.20,
        }
    )
    df = pd.DataFrame([row])
    out = nifty_2026_call_put_research(df)
    assert out["strategy_TrendPullbackCall_signal"].iloc[0] == CALL


def test_trend_pullback_call_allows_rsi_40_50_only_with_long_unwinding() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 102.0,
            "ma50": 100.0,
            "ma20_slope": 0.003,
            "rsi5": 45.0,
            "vix_close": 13.5,
            "oi_regime": "long_unwinding",
            "gap_open_atr": 0.20,
        }
    )
    out = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out["strategy_TrendPullbackCall_signal"].iloc[0] == CALL

    row["oi_regime"] = "short_buildup"
    out_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_blocked["strategy_TrendPullbackCall_signal"].iloc[0] == FLAT


def test_trend_pullback_call_requires_ret3d_minus_half_percent_or_below() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 102.0,
            "ma50": 100.0,
            "ma20_slope": 0.003,
            "rsi5": 35.0,
            "ret_3d": -0.004,
            "vix_close": 13.5,
            "oi_regime": "long_unwinding",
            "gap_open_atr": 0.20,
        }
    )
    out = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out["strategy_TrendPullbackCall_signal"].iloc[0] == FLAT


def test_downtrend_bounce_call_requires_selloff_and_gap_gate() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 99.0,
            "ma20": 100.0,
            "ma20_slope": -0.003,
            "rsi5": 25.0,
            "atr14": 2.0,
            "open_915": 100.0,
            "vix_close": 12.2,
            "gap_open_atr": 0.10,
        }
    )
    df = pd.DataFrame([row])
    out = nifty_2026_call_put_research(df)
    assert out["strategy_DowntrendBounceCall_signal"].iloc[0] == CALL

    row["gap_open_atr"] = 0.30
    out_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_blocked["strategy_DowntrendBounceCall_signal"].iloc[0] == FLAT


def test_reversal_call_requires_vix_and_gap_down_within_support_band() -> None:
    row = _base_row()
    row.update(
        {
            "support_distance_10d": 0.70,
            "rsi5": 27.0,
            "oi_regime": "short_buildup",
            "vix_close": 12.2,
            "gap_open_atr": -0.30,
        }
    )
    df = pd.DataFrame([row])
    out = nifty_2026_call_put_research(df)
    assert out["strategy_ReversalCall_signal"].iloc[0] == CALL

    row["support_distance_10d"] = -0.01
    out_negative_support_distance = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_negative_support_distance["strategy_ReversalCall_signal"].iloc[0] == CALL

    row["support_distance_10d"] = 0.80
    out_outside_band = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_outside_band["strategy_ReversalCall_signal"].iloc[0] == FLAT

    row["support_distance_10d"] = 0.70
    row["vix_close"] = 11.8
    out_low_vix = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_low_vix["strategy_ReversalCall_signal"].iloc[0] == FLAT


def test_downtrend_continuation_put_fires_and_blocks_short_covering() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 98.0,
            "ma50": 100.0,
            "ma20_slope": -0.002,
            "ret_5d": 0.009,
            "rsi5": 40.0,
            "vix_close": 13.4,
            "oi_regime": "short_buildup",
            "gap_open_atr": -0.10,
        }
    )
    df = pd.DataFrame([row])
    out = nifty_2026_call_put_research(df)
    assert out["strategy_DowntrendContinuationPut_signal"].iloc[0] == PUT

    row["oi_regime"] = "short_covering"
    out_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_blocked["strategy_DowntrendContinuationPut_signal"].iloc[0] == FLAT


def test_downtrend_continuation_put_blocks_when_ret5_or_rsi_outside_band() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 98.0,
            "ma50": 100.0,
            "ma20_slope": -0.002,
            "ret_5d": 0.012,
            "rsi5": 40.0,
            "vix_close": 13.4,
            "oi_regime": "short_buildup",
            "gap_open_atr": -0.10,
        }
    )
    out_ret_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_ret_blocked["strategy_DowntrendContinuationPut_signal"].iloc[0] == FLAT

    row["ret_5d"] = 0.009
    row["rsi5"] = 56.0
    out_rsi_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_rsi_blocked["strategy_DowntrendContinuationPut_signal"].iloc[0] == FLAT


def test_uptrend_exhaustion_put_fires() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 105.0,
            "ma20": 100.0,
            "ma20_slope": 0.003,
            "price_zscore_20d": 1.7,
            "pcr_oi": 1.12,
            "nifty_gap_pct": 0.002,
        }
    )
    out = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out["strategy_UptrendExhaustionPut_signal"].iloc[0] == PUT


def test_uptrend_exhaustion_put_requires_2p5pct_above_ma20_and_pcr_cap() -> None:
    row = _base_row()
    row.update(
        {
            "close_1515": 102.0,
            "ma20": 100.0,
            "ma20_slope": 0.003,
            "price_zscore_20d": 1.7,
            "pcr_oi": 1.12,
            "nifty_gap_pct": 0.002,
        }
    )
    out_not_stretched = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_not_stretched["strategy_UptrendExhaustionPut_signal"].iloc[0] == FLAT

    row["close_1515"] = 103.0
    row["pcr_oi"] = 1.16
    out_pcr_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_pcr_blocked["strategy_UptrendExhaustionPut_signal"].iloc[0] == FLAT


def test_reversal_put_fires_with_overbought_top_range_then_late_selloff() -> None:
    row = _base_row()
    row.update(
        {
            "range_position_20d": 0.82,
            "rsi5": 73.0,
            "pcr_oi": 1.10,
            "high_day": 104.0,
            "low_day": 100.0,
            "close_1415": 103.0,
            "close_1515": 101.5,
            "atr14": 2.0,
        }
    )
    out = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out["strategy_ReversalPut_signal"].iloc[0] == PUT


def test_reversal_put_blocks_when_any_new_gate_fails() -> None:
    row = _base_row()
    row.update(
        {
            "range_position_20d": 0.82,
            "rsi5": 73.0,
            "pcr_oi": 1.10,
            "high_day": 104.0,
            "low_day": 100.0,
            "close_1415": 103.0,
            "close_1515": 101.5,
            "atr14": 2.0,
        }
    )

    row["pcr_oi"] = 1.16
    out_pcr_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_pcr_blocked["strategy_ReversalPut_signal"].iloc[0] == FLAT

    row["pcr_oi"] = 1.10
    row["close_1515"] = 103.2
    out_close_loc_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_close_loc_blocked["strategy_ReversalPut_signal"].iloc[0] == FLAT

    row["close_1515"] = 102.8
    out_late_selloff_blocked = nifty_2026_call_put_research(pd.DataFrame([row]))
    assert out_late_selloff_blocked["strategy_ReversalPut_signal"].iloc[0] == FLAT
