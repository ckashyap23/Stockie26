from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from src.technical_analysis.strategy_families import get_strategy_family_registry

from .constants import CALL, FLAT, PUT, REGIME_THRESHOLD


CONFIG_PATH = Path(__file__).with_name("signal_strength_config.yaml")


@dataclass(frozen=True)
class CascadeSignalDetail:
    direction: str
    primary_strategy: str | None
    strategy_precision: float | None
    signal_style: str | None
    strength_score: float | None
    strength_label: str | None
    confidence_level: float | None


def enrich_option_signal_columns(
    df: pd.DataFrame,
    final_prediction: pd.Series,
    regime_signals: dict[str, dict[str, pd.Series]],
    eligibility: dict[str, tuple[dict[str, float], dict[str, float]]],
) -> pd.DataFrame:
    """Add option-ready signal metadata to the production cascade frame.

    The production contract intentionally uses `direction` as the cascade side
    (`CALL`, `PUT`, `NO_POSITION`) and does not emit a separate `raw_signal`.
    """
    out = df.copy()
    details = [
        cascade_signal_detail(idx, out, final_prediction, regime_signals, eligibility)
        for idx in out.index
    ]
    out["direction"] = [detail.direction for detail in details]
    out["volatility_regime"] = out["regime"]
    out["stock_regime"] = pd.NA
    out["primary_strategy"] = [detail.primary_strategy for detail in details]
    registry = get_strategy_family_registry()
    metas = [
        registry.get_meta(detail.primary_strategy)
        if detail.primary_strategy in (registry.variants | registry.guard_variants) else None
        for detail in details
    ]
    out["primary_strategy_family"] = [meta.family if meta else None for meta in metas]
    out["primary_strategy_type"] = [meta.strategy_type if meta else None for meta in metas]
    out["strategy_precision"] = [detail.strategy_precision for detail in details]
    out["signal_style"] = [detail.signal_style for detail in details]
    out["strength_score"] = [detail.strength_score for detail in details]
    out["strength_label"] = [detail.strength_label for detail in details]
    out["confidence_level"] = [detail.confidence_level for detail in details]

    # Reserved for richer option gating later. The current option selector does
    # not consume these fields.
    out["expected_move_pct"] = pd.NA
    out["is_option_eligible"] = pd.NA
    out["option_bias"] = pd.NA
    out["conflict_flag"] = pd.NA
    return out


def cascade_signal_detail(
    idx: Any,
    df: pd.DataFrame,
    final_prediction: pd.Series,
    regime_signals: dict[str, dict[str, pd.Series]],
    eligibility: dict[str, tuple[dict[str, float], dict[str, float]]],
) -> CascadeSignalDetail:
    direction = str(final_prediction.loc[idx])
    if direction not in {CALL, PUT}:
        return CascadeSignalDetail(direction=FLAT, primary_strategy=None, strategy_precision=None,
                                   signal_style=None, strength_score=None, strength_label=None,
                                   confidence_level=None)

    regime = str(df.loc[idx, "regime"])
    signals = regime_signals.get(regime, {})
    call_elig, put_elig = eligibility.get(regime, ({}, {}))
    side_elig = call_elig if direction == CALL else put_elig
    firing = [
        (name, precision)
        for name, precision in side_elig.items()
        if name in signals and signals[name].loc[idx] == direction
    ]
    if not firing:
        # eligibility dict not provided (live/batch pipeline always passes {}) —
        # scan regime_signals directly to identify the primary strategy so the
        # strength score can still be computed.
        _type_rank = {"TRADE_ELIGIBLE": 0, "SIGNAL": 1, "WATCH_ONLY": 2, "VOTE_ONLY": 3}
        _registry = get_strategy_family_registry()

        def _rank(n: str) -> int:
            try:
                return _type_rank.get(_registry.get_meta(n).strategy_type, 9)
            except KeyError:
                return 9

        fallback = [n for n, sig in signals.items() if sig.loc[idx] == direction]
        if not fallback:
            return CascadeSignalDetail(direction=direction, primary_strategy=None, strategy_precision=None,
                                       signal_style=None, strength_score=None, strength_label=None,
                                       confidence_level=None)
        primary_strategy = min(fallback, key=_rank)
        style = classify_signal_style(primary_strategy)
        score = score_signal_strength(df.loc[idx], direction, primary_strategy)
        return CascadeSignalDetail(
            direction=direction,
            primary_strategy=primary_strategy,
            strategy_precision=None,
            signal_style=style,
            strength_score=score,
            strength_label=strength_label(score),
            confidence_level=None,
        )

    primary_strategy, strategy_precision = max(firing, key=lambda item: item[1])
    style = classify_signal_style(primary_strategy)
    score = score_signal_strength(df.loc[idx], direction, primary_strategy)
    return CascadeSignalDetail(
        direction=direction,
        primary_strategy=primary_strategy,
        strategy_precision=round(float(strategy_precision), 4),
        signal_style=style,
        strength_score=score,
        strength_label=strength_label(score),
        confidence_level=round(float(strategy_precision), 4),
    )


def classify_signal_style(strategy_name: str | None) -> str | None:
    if not strategy_name:
        return None
    configured = _strategy_config(strategy_name).get("style")
    if configured:
        return str(configured)
    name = strategy_name.lower()
    if any(token in name for token in ("bounce", "rebound", "meanreversion", "fade", "oversold")):
        return "bounce"
    if any(token in name for token in ("momentum", "trend", "alignment", "breakout", "matrend")):
        return "trend_momentum"
    return "other"


def score_signal_strength(
    row: pd.Series,
    direction: str,
    strategy_name: str | None,
) -> float | None:
    if direction not in {CALL, PUT} or not strategy_name:
        return None

    strategy_cfg = _strategy_config(strategy_name)
    if not strategy_cfg:
        return None

    score = float(strategy_cfg.get("base_score", 0.0))
    adjustment_sets = _strength_config().get("adjustment_sets", {})
    for set_name in strategy_cfg.get("adjustments", []):
        for rule in adjustment_sets.get(set_name, []):
            score += _rule_adjustment(row, rule)

    return round(max(0.0, min(100.0, score)), 2)


def strength_label(score: float | None) -> str | None:
    if score is None:
        return None
    labels = _strength_config().get("labels", {})
    if score >= float(labels.get("strong_min", 80.0)):
        return "STRONG"
    if score >= float(labels.get("moderate_min", 65.0)):
        return "MODERATE"
    return "WEAK"


@lru_cache(maxsize=1)
def _strength_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data


def _strategy_config(strategy_name: str) -> dict[str, Any]:
    return _strength_config().get("strategies", {}).get(strategy_name, {})


def _rule_adjustment(row: pd.Series, rule: dict[str, Any]) -> float:
    value = _numeric(row.get(str(rule.get("column"))))
    if value is None:
        return 0.0
    if "gt_column" in rule:
        other = _numeric(row.get(str(rule["gt_column"])))
        return float(rule.get("add", 0.0)) if other is not None and value > other else 0.0
    threshold = _rule_threshold(row, rule)
    if threshold is None:
        return 0.0
    if "lt" in rule or "lt_regime_threshold_multiple" in rule:
        return float(rule.get("add", 0.0)) if value < threshold else 0.0
    if "lte" in rule:
        return float(rule.get("add", 0.0)) if value <= threshold else 0.0
    if "gt" in rule:
        return float(rule.get("add", 0.0)) if value > threshold else 0.0
    if "gte" in rule or "gte_regime_threshold_multiple" in rule:
        return float(rule.get("add", 0.0)) if value >= threshold else 0.0
    return 0.0


def _rule_threshold(row: pd.Series, rule: dict[str, Any]) -> float | None:
    if "lt" in rule:
        return float(rule["lt"])
    if "lte" in rule:
        return float(rule["lte"])
    if "gt" in rule:
        return float(rule["gt"])
    if "gte" in rule:
        return float(rule["gte"])
    if "lt_regime_threshold_multiple" in rule:
        return _regime_threshold(row) * float(rule["lt_regime_threshold_multiple"])
    if "gte_regime_threshold_multiple" in rule:
        return _regime_threshold(row) * float(rule["gte_regime_threshold_multiple"])
    return None


def _regime_threshold(row: pd.Series) -> float:
    return REGIME_THRESHOLD.get(str(row.get("regime")), 0.005)


def _numeric(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
