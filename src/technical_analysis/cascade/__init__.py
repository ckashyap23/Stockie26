"""NIFTY regime-aware precision cascade â€” shared engine + promoted strategy roster.

Public API re-exported for convenience. Two pipelines consume this package:
  * the research harness (backtest/vectorbt_research/strategy_grid.py), which registers
    the FULL strategy roster (promoted + experimental), and
  * production (src/technical_analysis/cascade/pipeline.py, scripts/daily_NIFTY),
    which registers ONLY the promoted roster.
Both share this engine; only the roster passed to `gather_regime_signals` differs.
"""
from __future__ import annotations

from . import constants, dataset, engine, strategies
from .constants import (
    FEATURE_STORE, CALL, PUT, FLAT, THRESHOLD,
    REGIME_CALM, REGIME_STRESS, REGIMES,
    REGIME_VIX_CUTOFF, REGIME_VOL_CUTOFF, REGIME_THRESHOLD,
    PRECISION_FLOOR, REGIME_PRECISION_FLOOR, MIN_FIRES, WF_WINDOW, WF_MIN_FIRES,
    _VIX_COLS, _BASE_STR_COLS, _DROP_EXACT,
)
from .dataset import (
    classify_regime, _label_at, _call_ok, _put_ok,
    load_vix, build_base, regime_frame,
)
from .engine import (
    Metrics, score_signal, _fmt,
    gather_regime_signals, build_regime_cascade, walk_forward_regime,
    score_final, _confusion_lines,
)
from .strategies import (
    PROMOTED_REGIME_FAMILIES, PROMOTED_STRESS_FAMILIES, PROMOTED_CALM_FAMILIES,
    PROMOTED_DEFINITIONS,
    WATCH_ONLY_REGIME_FAMILIES,
)

__all__ = [
    "constants", "dataset", "engine", "strategies",
    "FEATURE_STORE", "CALL", "PUT", "FLAT", "THRESHOLD",
    "REGIME_CALM", "REGIME_STRESS", "REGIMES",
    "REGIME_VIX_CUTOFF", "REGIME_VOL_CUTOFF", "REGIME_THRESHOLD",
    "PRECISION_FLOOR", "REGIME_PRECISION_FLOOR", "MIN_FIRES", "WF_WINDOW", "WF_MIN_FIRES",
    "_VIX_COLS", "_BASE_STR_COLS", "_DROP_EXACT",
    "classify_regime", "_label_at", "_call_ok", "_put_ok",
    "load_vix", "build_base", "regime_frame",
    "Metrics", "score_signal", "_fmt",
    "gather_regime_signals", "build_regime_cascade", "walk_forward_regime",
    "score_final", "_confusion_lines",
    "PROMOTED_REGIME_FAMILIES", "PROMOTED_STRESS_FAMILIES", "PROMOTED_CALM_FAMILIES",
    "PROMOTED_DEFINITIONS",
    "WATCH_ONLY_REGIME_FAMILIES",
]
