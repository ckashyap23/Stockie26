"""Watch promotion tests — aligned with the new SIGNAL/VOTE_ONLY/RESEARCH taxonomy
and the different-family confirmation rule (same family re-firing never promotes).
"""
import pandas as pd
import pytest

from src.technical_analysis.cascade.watch_promotion import add_watch_promotions
from src.technical_analysis.cascade.constants import CALL, PUT, FLAT


def _s(values, index=None):
    return pd.Series(values, index=index if index is not None else range(len(values)))


def _df(n, regime="calm", closes=None):
    d = {
        "signal_date": pd.date_range("2026-01-01", periods=n, freq="B"),
        "regime": [regime] * n,
    }
    if closes is not None:
        d["close_1515"] = closes
    return pd.DataFrame(d)


def _run(final, signals, regime="calm", closes=None):
    df = _df(len(final), regime, closes)
    rs = {regime: {k: _s(v) for k, v in signals.items()}}
    return add_watch_promotions(df, _s(final), rs)


# ── Seeding ───────────────────────────────────────────────────────────────────

def test_signal_type_seeds_watch():
    out = _run([FLAT], {"PullbackCall_TrendIntact": [CALL]})
    assert out.loc[0, "watch_signal"] == "CALL_3D_WATCH"
    assert out.loc[0, "watch_family"] == "PullbackCall"
    assert out.loc[0, "watch_strategy_type"] == "SIGNAL"


def test_research_type_cannot_seed_watch():
    out = _run([FLAT], {"FastDropPut_5d": [PUT]})
    assert pd.isna(out.loc[0, "watch_signal"])


def test_actionable_final_prediction_blocks_watch_creation():
    out = _run([CALL], {"PullbackCall_TrendIntact": [CALL]})
    assert pd.isna(out.loc[0, "watch_signal"])
    assert out.loc[0, "promotion_reason"] == "FINAL_PREDICTION_ALREADY_ACTIONABLE"


def test_conflicting_d0_directions_block_watch():
    out = _run(
        [FLAT],
        {"PullbackCall_TrendIntact": [CALL], "DeclineContinuationPut_ATR": [PUT]},
    )
    assert pd.isna(out.loc[0, "watch_signal"])
    assert out.loc[0, "promotion_reason"] == "WATCH_CONFLICT_BOTH_DIRECTIONS"


# ── Same-family self-promotion bug fix ───────────────────────────────────────

def test_same_family_refire_does_not_self_promote_on_d1():
    """Core bug fix: same family re-firing on D1 must not bypass the different-family rule."""
    out = _run(
        [FLAT, FLAT],
        {"PullbackCall_TrendIntact": [CALL, CALL]},
        closes=[100.0, 101.0],
    )
    assert out.loc[0, "watch_signal"] == "CALL_3D_WATCH"
    assert out.loc[1, "promoted_prediction"] == FLAT
    assert out.loc[1, "promotion_reason"] == "NO_DIFFERENT_FAMILY_CONFIRMATION"


def test_multiple_same_family_variants_still_blocked():
    """Two variants from the same family on D1 still cannot confirm the D0 watch."""
    out = _run(
        [FLAT, FLAT],
        {
            "PullbackCall_TrendIntact": [CALL, CALL],
            "PullbackCall_TrendRest":   [FLAT, CALL],
        },
        closes=[100.0, 101.0],
    )
    assert out.loc[1, "promoted_prediction"] == FLAT
    assert out.loc[1, "promotion_reason"] == "NO_DIFFERENT_FAMILY_CONFIRMATION"


# ── Legitimate confirmation ───────────────────────────────────────────────────

def test_different_family_confirms_on_d1():
    """A genuinely different family on D1 promotes the watch."""
    out = _run(
        [FLAT, FLAT],
        {
            "PullbackCall_TrendIntact": [CALL, FLAT],
            "RsiReversion_6040":        [FLAT, CALL],
        },
    )
    assert out.loc[1, "promoted_prediction"] == CALL
    assert out.loc[1, "promotion_reason"].startswith(
        "PROMOTED_BY_DIFFERENT_FAMILY:PullbackCall:"
    )


def test_price_action_disabled_does_not_promote_on_d1():
    """Price-action promotion is disabled globally (watch_only_price_action_promotion:
    enabled: false).  A watch whose D1 has no confirming family must not promote,
    even when price moved in the right direction."""
    out = _run(
        [FLAT, FLAT],
        {"PullbackCall_TrendIntact": [CALL, FLAT]},
        closes=[100.0, 101.0],
    )
    assert out.loc[1, "promoted_prediction"] == FLAT
    assert out.loc[1, "promotion_reason"] == "WATCH_ACTIVE_AWAITING_CONFIRMATION"
    assert pd.isna(out.loc[1, "confirming_variant"])


def test_price_action_disabled_watch_expires_without_promotion():
    """With price-action off, a watch that never gets a different-family confirmer
    simply expires after the horizon — even with favourable price moves every day."""
    out = _run(
        [FLAT, FLAT, FLAT],
        {"PullbackCall_TrendIntact": [CALL, FLAT, FLAT]},
        closes=[100.0, 101.0, 102.0],
    )
    assert out.loc[2, "promoted_prediction"] == FLAT
    assert out.loc[2, "promotion_reason"] == "WATCH_EXPIRED_NO_CONFIRMATION"


def test_watch_ages_and_promotes_on_d2_by_different_family():
    """D1 miss → watch still active → D2 different family confirms."""
    out = _run(
        [FLAT, FLAT, FLAT],
        {
            "PullbackCall_TrendIntact": [CALL, FLAT, FLAT],
            "RsiReversion_6040":        [FLAT, FLAT, CALL],
        },
    )
    assert out.loc[1, "promotion_reason"] == "WATCH_ACTIVE_AWAITING_CONFIRMATION"
    assert out.loc[2, "prior_watch_age"] == 2
    assert out.loc[2, "promoted_prediction"] == CALL


# ── Opposition ────────────────────────────────────────────────────────────────

def test_signal_opposition_kills_watch_immediately():
    """A SIGNAL-type strategy on the opposite side = strong opposition → watch killed."""
    out = _run(
        [FLAT, FLAT],
        {
            "PullbackCall_TrendIntact":  [CALL, FLAT],
            "DeclineContinuationPut_ATR": [FLAT, PUT],
        },
    )
    assert out.loc[1, "promoted_prediction"] == FLAT
    assert out.loc[1, "promotion_reason"] == "WATCH_KILLED_STRONG_OPPOSITION"


def test_vote_only_opposition_is_weak_watch_survives():
    """A VOTE_ONLY dissenter on the opposite side is weak — watch ages rather than dies."""
    out = _run(
        [FLAT, FLAT, FLAT],
        {
            "PullbackCall_TrendIntact": [CALL, FLAT, FLAT],
            "RsiReversion_6040":        [FLAT, PUT,  FLAT],  # VOTE_ONLY opp on D1
        },
    )
    assert out.loc[1, "promotion_reason"] == "WATCH_ACTIVE_AWAITING_CONFIRMATION"


# ── Expiry ────────────────────────────────────────────────────────────────────

def test_watch_expires_after_d2_without_confirmation():
    out = _run(
        [FLAT] * 4,
        {"PullbackCall_TrendIntact": [CALL, FLAT, FLAT, CALL]},
    )
    assert out.loc[2, "promotion_reason"] == "WATCH_EXPIRED_NO_CONFIRMATION"
    # D3: fresh watch can start again
    assert pd.isna(out.loc[3, "prior_watch_signal"])
    assert out.loc[3, "watch_signal"] == "CALL_3D_WATCH"


# ── Range breakout special rules ──────────────────────────────────────────────

def test_d2_range_breakout_call_requires_range_family_confirmation():
    """On D2, a RangeBreakout-named CALL watch must be confirmed by a RangeBreakout family."""
    out = _run(
        [FLAT, FLAT, FLAT],
        {
            "RangeBreakoutCandidate":   [CALL, FLAT, FLAT],
            "PullbackCall_TrendIntact": [FLAT, FLAT, CALL],   # different non-range family
        },
    )
    assert out.loc[2, "promoted_prediction"] == FLAT
    assert out.loc[2, "promotion_reason"] == "RANGEBREAKOUT_CALL_WATCH_EXPIRED_NO_D2_CONFIRMATION"


def test_range_breakout_watch_expires_when_breakout_level_reclaimed():
    df = pd.DataFrame({
        "signal_date":  pd.date_range("2026-01-01", periods=2, freq="B"),
        "regime":       ["stress", "stress"],
        "close_1515":   [101.0, 99.5],
        "recent_high_20d": [100.0, 100.0],
    })
    signals = {"stress": {"RangeBreakoutCandidate": _s([CALL, CALL])}}
    out = add_watch_promotions(df, _s([FLAT, FLAT]), signals)
    assert out.loc[1, "promotion_reason"] == "RANGE_WATCH_EXPIRED_BROKEN_LEVEL_RECLAIMED"



