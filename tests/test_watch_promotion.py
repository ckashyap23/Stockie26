import pandas as pd

from src.technical_analysis.cascade.watch_promotion import add_watch_promotions


def _series(values):
    return pd.Series(values, index=range(len(values)))


def _run(final, call_values, put_values=None, dates=None):
    n = len(final)
    df = pd.DataFrame({
        "signal_date": dates or pd.date_range("2026-01-01", periods=n, freq="B"),
        "regime": ["stress"] * n,
    })
    signals = {"stress": {"call_strategy": _series(call_values)}}
    if put_values is not None:
        signals["stress"]["put_strategy"] = _series(put_values)
    return add_watch_promotions(df, _series(final), signals)


def test_d1_same_direction_confirmation_promotes():
    out = _run(["NO_POSITION", "NO_POSITION"], ["CALL", "CALL"])
    assert out.loc[0, "watch_signal"] == "CALL_3D_WATCH"
    assert out.loc[1, "prior_watch_signal"] == "CALL_3D_WATCH"
    assert out.loc[1, "prior_watch_age"] == 1
    assert out.loc[1, "promoted_prediction"] == "CALL"
    assert out.loc[1, "promotion_reason"].startswith("PROMOTED_BY_SAME_FAMILY:")


def test_watch_only_d0_can_be_promoted_by_trade_eligible_same_family():
    df = pd.DataFrame({"regime": ["stress", "stress"]})
    signals = {"stress": {
        "OversoldBounceCall_MoreTrades": _series(["CALL", "NO_POSITION"]),
        "OversoldBounceCall_HighPrecision": _series(["NO_POSITION", "CALL"]),
    }}
    out = add_watch_promotions(df, _series(["NO_POSITION", "NO_POSITION"]), signals)
    assert out.loc[0, "watch_strategy_type"] == "WATCH_ONLY"
    assert out.loc[1, "prior_watch_strategy_type"] == "WATCH_ONLY"
    assert out.loc[1, "confirming_strategy_type"] == "TRADE_ELIGIBLE"
    assert out.loc[1, "promoted_prediction"] == "CALL"


def test_watch_only_cannot_confirm_without_price_action():
    df = pd.DataFrame({"regime": ["stress", "stress"]})
    signals = {"stress": {
        "OversoldBounceCall_MoreTrades": _series(["CALL", "CALL"]),
    }}
    out = add_watch_promotions(df, _series(["NO_POSITION", "NO_POSITION"]), signals)
    assert out.loc[1, "promoted_prediction"] == "NO_POSITION"
    assert out.loc[1, "promotion_reason"] == "NO_SAME_FAMILY_CONFIRMATION"


def test_watch_only_promotes_on_same_family_price_action_confirmation():
    df = pd.DataFrame({
        "regime": ["stress", "stress"],
        "close_1515": [100.0, 101.0],
    })
    signals = {"stress": {
        "OversoldBounceCall_MoreTrades": _series(["CALL", "NO_POSITION"]),
    }}
    out = add_watch_promotions(df, _series(["NO_POSITION", "NO_POSITION"]), signals)
    assert out.loc[1, "confirming_variant"] == "OversoldBounceCall_MoreTrades"
    assert out.loc[1, "confirming_strategy_type"] == "WATCH_ONLY"
    assert out.loc[1, "promoted_prediction"] == "CALL"


def test_research_strategy_cannot_create_watch():
    df = pd.DataFrame({"regime": ["stress"]})
    signals = {"stress": {"MomentumDirectional": _series(["CALL"])}}
    out = add_watch_promotions(df, _series(["NO_POSITION"]), signals)
    assert pd.isna(out.loc[0, "watch_signal"])


def test_d2_confirmation_uses_trading_sessions_not_calendar_days():
    dates = [pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
    out = _run(
        ["NO_POSITION"] * 3,
        ["PUT", "NO_POSITION", "PUT"],
        dates=dates,
    )
    assert out.loc[2, "prior_watch_age"] == 2
    assert out.loc[2, "promoted_prediction"] == "PUT"


def test_watch_expires_without_confirmation():
    out = _run(["NO_POSITION"] * 4, ["CALL", "NO_POSITION", "NO_POSITION", "CALL"])
    assert out.loc[2, "promotion_reason"] == "WATCH_EXPIRED_NO_CONFIRMATION"
    assert pd.isna(out.loc[3, "prior_watch_signal"])
    assert out.loc[3, "watch_signal"] == "CALL_3D_WATCH"


def test_conflicting_d0_signals_do_not_create_watch():
    out = _run(["NO_POSITION"], ["CALL"], ["PUT"])
    assert pd.isna(out.loc[0, "watch_signal"])
    assert out.loc[0, "promotion_reason"] == "WATCH_CONFLICT_BOTH_DIRECTIONS"


def test_opposite_confirmation_does_not_promote():
    out = _run(
        ["NO_POSITION", "NO_POSITION"],
        ["CALL", "NO_POSITION"],
        ["NO_POSITION", "PUT"],
    )
    assert out.loc[1, "promoted_prediction"] == "NO_POSITION"
    assert out.loc[1, "promotion_reason"] == "WATCH_REJECTED_OPPOSITE_CONFIRMATION"


def test_actionable_final_prediction_does_not_start_watch():
    out = _run(["CALL"], ["CALL"])
    assert pd.isna(out.loc[0, "watch_signal"])
    assert out.loc[0, "promotion_reason"] == "FINAL_PREDICTION_ALREADY_ACTIONABLE"


def test_put_squeeze_profile_vetoes_promotion():
    df = pd.DataFrame({
        "signal_date": pd.date_range("2026-01-01", periods=2, freq="B"),
        "regime": ["stress", "stress"],
        "vix_chg_pct": [0.0, -0.06],
        "ret_3d": [-0.01, 0.01],
        "range_position_10d": [0.3, 0.85],
    })
    signals = {"stress": {"put_strategy": _series(["PUT", "PUT"])}}
    out = add_watch_promotions(df, _series(["NO_POSITION", "NO_POSITION"]), signals)
    assert out.loc[1, "promoted_prediction"] == "NO_POSITION"
    assert out.loc[1, "promotion_reason"] == "PUT_PROMOTION_VETO_BULLISH_SQUEEZE_RISK"


def test_oversold_call_low_volume_and_width_stays_on_watch():
    df = pd.DataFrame({
        "regime": ["stress", "stress"],
        "volume_hybrid": [1.0, 0.79],
        "bb_width": [0.06, 0.054],
    })
    signals = {"stress": {"OversoldBounceCall_HighPrecision": _series(["CALL", "CALL"])}}
    out = add_watch_promotions(df, _series(["NO_POSITION", "NO_POSITION"]), signals)
    assert out.loc[1, "promoted_prediction"] == "NO_POSITION"
    assert out.loc[1, "prior_watch_signal"] == "CALL_3D_WATCH"
    assert out.loc[1, "promotion_reason"] == "CALL_PROMOTION_VETO_LOW_VOLUME_LOW_BB_WIDTH"


def test_d2_range_breakout_requires_range_breakout_confirmation():
    df = pd.DataFrame({"regime": ["stress"] * 3})
    signals = {"stress": {
        "RangeBreakoutCandidate": _series(["CALL", "NO_POSITION", "NO_POSITION"]),
        "OtherCall": _series(["NO_POSITION", "NO_POSITION", "CALL"]),
    }}
    out = add_watch_promotions(df, _series(["NO_POSITION"] * 3), signals)
    assert out.loc[2, "promoted_prediction"] == "NO_POSITION"
    assert out.loc[2, "promotion_reason"] == "RANGEBREAKOUT_CALL_WATCH_EXPIRED_NO_D2_CONFIRMATION"


def test_put_positive_choppy_midrange_profile_vetoes_promotion():
    df = pd.DataFrame({
        "regime": ["stress", "stress"],
        "ret_3d": [-0.01, 0.0],
        "ret_5d": [-0.01, 0.01],
        "trend_efficiency_10d": [0.2, 0.09],
        "range_position_10d": [0.3, 0.60],
        "vix_chg_pct": [0.0, 0.0],
    })
    signals = {"stress": {"put_strategy": _series(["PUT", "PUT"])}}
    out = add_watch_promotions(df, _series(["NO_POSITION", "NO_POSITION"]), signals)
    assert out.loc[1, "promoted_prediction"] == "NO_POSITION"
    assert out.loc[1, "promotion_reason"] == "PUT_PROMOTION_VETO_POSITIVE_CHOPPY_MIDRANGE"


def test_range_breakout_watch_expires_when_d0_level_is_reclaimed():
    df = pd.DataFrame({
        "regime": ["stress", "stress"],
        "close_1515": [101.0, 99.5],
        "recent_high_20d": [100.0, 100.0],
    })
    signals = {"stress": {
        "RangeBreakoutCall_GlobalRiskAgree": _series(["CALL", "CALL"]),
    }}
    out = add_watch_promotions(df, _series(["NO_POSITION", "NO_POSITION"]), signals)
    assert out.loc[1, "promoted_prediction"] == "NO_POSITION"
    assert out.loc[1, "promotion_reason"] == "RANGE_WATCH_EXPIRED_BROKEN_LEVEL_RECLAIMED"


def test_range_breakdown_d2_can_confirm_when_level_was_never_clearly_reclaimed():
    df = pd.DataFrame({
        "regime": ["stress", "stress", "stress"],
        "close_1515": [99.0, 100.05, 99.8],
        "recent_low_20d": [100.0, 100.0, 100.0],
    })
    signals = {"stress": {
        "RangeBreakoutPut_GlobalAllDisagree": _series(["PUT", "NO_POSITION", "NO_POSITION"]),
    }}
    out = add_watch_promotions(df, _series(["NO_POSITION"] * 3), signals)
    assert out.loc[1, "promotion_reason"] == "WATCH_ACTIVE_AWAITING_CONFIRMATION"
    assert out.loc[2, "prior_watch_age"] == 2
    assert out.loc[2, "promoted_prediction"] == "PUT"
