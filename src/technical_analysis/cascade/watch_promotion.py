"""Watch-and-promotion layer for otherwise-flat cascade predictions.

This module deliberately does not alter ``final_prediction``.  It uses only
point-in-time strategy signals, so effective_prediction can consume the resulting
promotion without introducing look-ahead data into the production decision.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.technical_analysis.cascade.constants import CALL, FLAT, PUT
from src.technical_analysis.strategy_families import (
    StrategyMeta,
    get_strategy_family_registry,
)


CALL_WATCH = "CALL_3D_WATCH"
PUT_WATCH = "PUT_3D_WATCH"
WATCH_HORIZON_DAYS = 2  # D0 setup may be confirmed on trading session D1 or D2.


def _family_fired(strategy_names: tuple[str, ...] | list[str], family: str) -> bool:
    return any(family.lower() in name.lower() for name in strategy_names)


@dataclass
class _ActiveWatch:
    direction: str
    created_position: int
    family: str
    variant: str
    strategy_type: str
    breakout_level: float | None = None


def _d0_breakout_level(df: pd.DataFrame, position: int, direction: str) -> float | None:
    """Return the 20-session boundary broken on D0, excluding the D0 candle."""
    column = "low_day" if direction == PUT else "high_day"
    if column in df.columns and position > 0:
        history = pd.to_numeric(
            df.iloc[max(0, position - 20):position][column], errors="coerce"
        ).dropna()
        if not history.empty:
            return float(history.min() if direction == PUT else history.max())
    fallback = "recent_low_20d" if direction == PUT else "recent_high_20d"
    value = pd.to_numeric(pd.Series([df.iloc[position].get(fallback)]), errors="coerce").iloc[0]
    return None if pd.isna(value) else float(value)


def _range_level_reclaimed(row: pd.Series, active: _ActiveWatch) -> bool:
    if active.breakout_level is None or "range" not in active.family.lower():
        return False
    close = pd.to_numeric(pd.Series([row.get("close_1515")]), errors="coerce").iloc[0]
    if pd.isna(close):
        return False
    return bool(
        close > active.breakout_level * 1.001
        if active.direction == PUT
        else close < active.breakout_level * 0.999
    )


def _watch_price_action_confirms(
    df: pd.DataFrame, position: int, active: _ActiveWatch
) -> bool:
    """Confirm an active WATCH_ONLY setup using family-bound D1/D2 price action."""
    close = pd.to_numeric(
        pd.Series([df.iloc[position].get("close_1515")]), errors="coerce"
    ).iloc[0]
    if pd.isna(close):
        return False
    if active.breakout_level is not None and "range" in active.family.lower():
        return bool(
            close < active.breakout_level
            if active.direction == PUT
            else close > active.breakout_level
        )
    if position < 1:
        return False
    prior_close = pd.to_numeric(
        pd.Series([df.iloc[position - 1].get("close_1515")]), errors="coerce"
    ).iloc[0]
    if pd.isna(prior_close):
        return False
    return bool(close > prior_close if active.direction == CALL else close < prior_close)


def _meta_or_default(variant: str) -> StrategyMeta:
    registry = get_strategy_family_registry()
    try:
        return registry.get_meta(variant)
    except KeyError:  # Supports isolated/custom strategy tests and extensions.
        return StrategyMeta(
            variant=variant, family=variant, direction="TWO_SIDED",
            strategy_type="TRADE_ELIGIBLE", definition="",
        )


def _is_weak_opposition(names: list[str], registry: StrategyFamilyRegistry) -> bool:
    """Opposition is WEAK when ≤ 1 distinct family voted against AND none is SIGNAL.

    Family-level deduplication: 3 variants from the same family on the opposite
    side still count as 1 family — still weak.
    A single noisy VOTE_ONLY dissenter is not real disagreement.
    2+ families, or any SIGNAL (including legacy TE/WO) family, is STRONG.
    """
    families: dict[str, str] = {}
    for name in names:
        try:
            meta = registry.get_meta(name)
        except KeyError:
            continue
        families[meta.family] = meta.strategy_type
    return (
        len(families) <= 1
        and not any(
            t in {"SIGNAL", "TRADE_ELIGIBLE", "WATCH_ONLY"}
            for t in families.values()
        )
    )


def _best_family_representative(
    names: list[str],
    direction: str,
    precision: dict[str, float] | None = None,
    family: str | None = None,
    exclude_family: str | None = None,
    confirmation: bool = False,
    enforce_authority: bool = True,
) -> tuple[str, StrategyMeta] | None:
    candidates: list[tuple[float, int, str, StrategyMeta]] = []
    type_rank = {"TRADE_ELIGIBLE": 2, "WATCH_ONLY": 1}
    for name in names:
        meta = _meta_or_default(name)
        registry = get_strategy_family_registry()
        valid, _ = registry.validate_direction(name, direction) if name in (registry.variants | registry.guard_variants) else (True, "OK")
        if not valid or (family is not None and meta.family != family):
            continue
        if exclude_family is not None and meta.family == exclude_family:
            continue  # different-family confirmation: skip the watch-seeding family
        allowed = (
            meta.can_confirm_watch if confirmation else meta.can_create_watch
        ) if enforce_authority else meta.strategy_type != "RESEARCH"
        if not allowed:
            continue
        candidates.append((
            float((precision or {}).get(name, 0.0)),
            type_rank.get(meta.strategy_type, 0),
            name,
            meta,
        ))
    if not candidates:
        return None
    _, _, name, meta = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    return name, meta


def _fires_for_row(
    idx: object,
    regime: str,
    regime_signals: dict[str, dict[str, pd.Series]],
) -> tuple[list[str], list[str]]:
    """Return the CALL and PUT strategy names firing in the row's regime."""
    call_names: list[str] = []
    put_names: list[str] = []
    for name, signal in regime_signals.get(regime, {}).items():
        value = signal.get(idx, FLAT)
        if value == CALL:
            call_names.append(name)
        elif value == PUT:
            put_names.append(name)
    return sorted(call_names), sorted(put_names)


def add_watch_promotions(
    df: pd.DataFrame,
    final_prediction: pd.Series,
    regime_signals: dict[str, dict[str, pd.Series]],
    *,
    watch_horizon_days: int = WATCH_HORIZON_DAYS,
    strategy_precisions: dict[str, dict[str, float]] | None = None,
    enforce_strategy_policy: bool = True,
    cooloff_families: dict | None = None,
    vote_only_watch_seeds: dict | None = None,
) -> pd.DataFrame:
    """Add watch/promotion audit fields without changing the base prediction.

    Step 3 (different-family confirmation): a live watch from D-1/D-2 promotes
    when a *different* family votes the same side on D (same-family re-fire no
    longer promotes — that is one opinion repeating itself).

    ``vote_only_watch_seeds`` (optional) — from build_family_vote_cascade.
    When all cascade voters were VOTE_ONLY (no SIGNAL trade source), the
    cascade seeds a watch here even though no SIGNAL family fired.

    ``cooloff_families`` (optional) — blocks watch creation AND promotion for
    families currently in cooldown.
    """
    if watch_horizon_days < 1:
        raise ValueError("watch_horizon_days must be at least 1")
    if "regime" not in df.columns:
        raise ValueError("df must contain a regime column")
    if len(final_prediction) != len(df):
        raise ValueError("final_prediction must have the same length as df")
    out = df.copy()
    predictions = final_prediction.reindex(out.index).fillna(FLAT)
    out["watch_signal"] = None
    out["prior_watch_signal"] = None
    out["prior_watch_age"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["promoted_prediction"] = FLAT
    out["promotion_reason"] = "NO_WATCH_NO_STRATEGY_FIRE"
    for column in (
        "watch_family", "watch_variant", "watch_strategy_type",
        "prior_watch_family", "prior_watch_variant", "prior_watch_strategy_type",
        "confirming_family", "confirming_variant", "confirming_strategy_type",
        "promotion_block_reason",
    ):
        out[column] = None
    out["family_confirmation_match"] = False

    active: _ActiveWatch | None = None
    for position, idx in enumerate(out.index):
        regime = str(out.at[idx, "regime"])
        call_names, put_names = _fires_for_row(idx, regime, regime_signals)

        # Existing watches are evaluated before a new D0 watch can be created,
        # preventing a setup from confirming itself on the same session.
        if active is not None:
            age = position - active.created_position
            if age <= watch_horizon_days:
                # Block promotion if the active watch's family is in cooldown.
                cooled = (cooloff_families or {}).get(idx, frozenset())
                if active.family in cooled:
                    out.at[idx, "promotion_reason"] = f"WATCH_PROMOTION_BLOCKED_COOLOFF:{active.family}"
                    out.at[idx, "promotion_block_reason"] = out.at[idx, "promotion_reason"]
                    if age >= watch_horizon_days:
                        active = None
                    continue
                watch_value = CALL_WATCH if active.direction == CALL else PUT_WATCH
                out.at[idx, "prior_watch_signal"] = watch_value
                out.at[idx, "prior_watch_age"] = age
                out.at[idx, "prior_watch_family"] = active.family
                out.at[idx, "prior_watch_variant"] = active.variant
                out.at[idx, "prior_watch_strategy_type"] = active.strategy_type
                if _range_level_reclaimed(out.loc[idx], active):
                    reason = "RANGE_WATCH_EXPIRED_BROKEN_LEVEL_RECLAIMED"
                    out.at[idx, "promotion_reason"] = reason
                    out.at[idx, "promotion_block_reason"] = reason
                    active = None
                    continue
                same_names = call_names if active.direction == CALL else put_names
                opposite_names = put_names if active.direction == CALL else call_names
                _reg = get_strategy_family_registry()
                is_weak_opp = _is_weak_opposition(opposite_names, _reg)
                confirming = _best_family_representative(
                    same_names,
                    active.direction,
                    (strategy_precisions or {}).get(regime),
                    family=None,
                    exclude_family=active.family,   # Step 3: different family required
                    confirmation=True,
                    enforce_authority=enforce_strategy_policy,
                )
                _price_action_confirmed = False
                if (
                    confirming is None
                    and active.strategy_type in {"WATCH_ONLY", "SIGNAL"}
                    and is_weak_opp
                    and not any(
                        _meta_or_default(n).family == active.family
                        for n in same_names
                    )  # seeder's own family re-firing is not a confirmation
                    and get_strategy_family_registry().allow_watch_only_price_action_promotion()
                    and _watch_price_action_confirms(out, position, active)
                ):
                    _price_action_confirmed = True
                    confirming = (active.variant, _meta_or_default(active.variant))

                if confirming is not None and is_weak_opp:
                    # Promote: different family confirmed + weak opposition
                    confirming_name, confirming_meta = confirming
                    if _price_action_confirmed:
                        # Price action is its own evidence category — null out confirming
                        # fields so the seeder's variant does not echo back as the
                        # "confirmer" (that echo was making price-action promotions look
                        # like same-family re-fires in audit logs).
                        out.at[idx, "confirming_family"] = None
                        out.at[idx, "confirming_variant"] = None
                        out.at[idx, "confirming_strategy_type"] = None
                        out.at[idx, "family_confirmation_match"] = False
                        out.at[idx, "promoted_prediction"] = active.direction
                        out.at[idx, "promotion_reason"] = (
                            f"PROMOTED_BY_PRICE_ACTION:{active.family}:{active.variant}"
                        )
                        active = None
                        continue
                    out.at[idx, "confirming_family"] = confirming_meta.family
                    out.at[idx, "confirming_variant"] = confirming_name
                    out.at[idx, "confirming_strategy_type"] = confirming_meta.strategy_type
                    out.at[idx, "family_confirmation_match"] = True
                    if (
                        active.direction == CALL
                        and age == 2
                        and "rangebreakout" in active.family.lower()
                        and "rangebreakout" not in confirming_meta.family.lower()
                    ):
                        out.at[idx, "promotion_reason"] = "RANGEBREAKOUT_CALL_WATCH_EXPIRED_NO_D2_CONFIRMATION"
                        out.at[idx, "promotion_block_reason"] = out.at[idx, "promotion_reason"]
                        active = None
                        continue
                    out.at[idx, "promoted_prediction"] = active.direction
                    out.at[idx, "promotion_reason"] = (
                        f"PROMOTED_BY_DIFFERENT_FAMILY:{active.family}:{confirming_name}"
                    )
                    active = None
                    continue
                # Strong opposition kills the watch immediately.
                if opposite_names and not is_weak_opp:
                    reason = "WATCH_KILLED_STRONG_OPPOSITION"
                    out.at[idx, "promotion_reason"] = reason
                    out.at[idx, "promotion_block_reason"] = reason
                    active = None
                    continue
                # Weak or no opposition but no confirmer yet — watch ages.
                if confirming is None and same_names:
                    reason = (
                        "RANGEBREAKOUT_CALL_WATCH_EXPIRED_NO_D2_CONFIRMATION"
                        if age == 2 and active.direction == CALL
                        and "rangebreakout" in active.family.lower()
                        else "NO_DIFFERENT_FAMILY_CONFIRMATION"
                    )
                    out.at[idx, "promotion_reason"] = reason
                    out.at[idx, "promotion_block_reason"] = reason
                elif age == watch_horizon_days:
                    out.at[idx, "promotion_reason"] = "WATCH_EXPIRED_NO_CONFIRMATION"
                else:
                    out.at[idx, "promotion_reason"] = "WATCH_ACTIVE_AWAITING_CONFIRMATION"

                if age < watch_horizon_days:
                    continue
                active = None
                continue
            active = None

        if predictions.at[idx] != FLAT:
            out.at[idx, "promotion_reason"] = "FINAL_PREDICTION_ALREADY_ACTIONABLE"
            continue
        # D0 conflict check with weak-opposition rule.
        # A weak dissenter (at most 1 VOTE_ONLY family) does not block watch creation.
        if call_names and put_names:
            _reg_d0 = get_strategy_family_registry()
            put_is_weak  = _is_weak_opposition(put_names,  _reg_d0)
            call_is_weak = _is_weak_opposition(call_names, _reg_d0)
            if put_is_weak and not call_is_weak:
                put_names = []   # suppress weak PUT dissent; allow CALL watch
            elif call_is_weak and not put_is_weak:
                call_names = []  # suppress weak CALL dissent; allow PUT watch
            else:
                # Both strong, or both weak (1 VOTE_ONLY each side) — true conflict.
                out.at[idx, "promotion_reason"] = "WATCH_CONFLICT_BOTH_DIRECTIONS"
                continue

        # VOTE_ONLY consensus watch seeds (from build_family_vote_cascade):
        # force-create a watch even though no SIGNAL family is present.
        if (vote_only_watch_seeds or {}).get(idx) is not None and active is None:
            vo_direction, vo_family, vo_variant = vote_only_watch_seeds[idx]
            cooled_d0 = (cooloff_families or {}).get(idx, frozenset())
            if vo_family not in cooled_d0:
                vo_meta = _meta_or_default(vo_variant)
                watch_value = CALL_WATCH if vo_direction == CALL else PUT_WATCH
                out.at[idx, "watch_signal"] = watch_value
                out.at[idx, "watch_family"] = vo_family
                out.at[idx, "watch_variant"] = vo_variant
                out.at[idx, "watch_strategy_type"] = vo_meta.strategy_type
                out.at[idx, "promotion_reason"] = (
                    f"WATCH_CREATED_{vo_direction}:VOTE_CONSENSUS:{vo_family}:{vo_variant}"
                )
                active = _ActiveWatch(
                    vo_direction, position, vo_family, vo_variant,
                    vo_meta.strategy_type, None,
                )
            continue

        if call_names or put_names:
            direction = CALL if call_names else PUT
            names = call_names if call_names else put_names
            # Block watch creation if the best family is in cooldown.
            cooled_d0 = (cooloff_families or {}).get(idx, frozenset())
            representative = _best_family_representative(
                names, direction, (strategy_precisions or {}).get(regime)
                , enforce_authority=enforce_strategy_policy
            )
            if representative is not None and representative[1].family in cooled_d0:
                out.at[idx, "promotion_reason"] = f"WATCH_CREATION_BLOCKED_COOLOFF:{representative[1].family}"
                continue
            if representative is None:
                out.at[idx, "promotion_reason"] = "NO_WATCH_ELIGIBLE_STRATEGY_FAMILY"
                continue
            variant, meta = representative
            watch_value = CALL_WATCH if direction == CALL else PUT_WATCH
            out.at[idx, "watch_signal"] = watch_value
            out.at[idx, "watch_family"] = meta.family
            out.at[idx, "watch_variant"] = variant
            out.at[idx, "watch_strategy_type"] = meta.strategy_type
            out.at[idx, "promotion_reason"] = (
                f"WATCH_CREATED_{direction}:{meta.family}:{variant}"
            )
            active = _ActiveWatch(
                direction, position, meta.family, variant, meta.strategy_type,
                _d0_breakout_level(out, position, direction)
                if "range" in meta.family.lower() else None,
            )

    return out
