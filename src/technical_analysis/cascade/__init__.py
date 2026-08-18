"""NIFTY precision cascade shared engine and strategy roster."""
from __future__ import annotations

from . import constants, dataset, engine, strategies
from .constants import (
    FEATURE_STORE, CALL, PUT, FLAT, THRESHOLD, TARGET_THRESHOLD,
    PRECISION_FLOOR, MIN_FIRES, WF_WINDOW, WF_MIN_FIRES,
    _VIX_COLS, _BASE_STR_COLS, _DROP_EXACT,
)
from .dataset import _label_at, _call_ok, _put_ok, load_vix, build_base, scoring_frame
from .engine import (
    Metrics, score_signal, _fmt,
    gather_signals, build_cascade, walk_forward,
    score_final, _confusion_lines,
)
from .strategies import (
    PROMOTED_FAMILIES, PROMOTED_DEFINITIONS,
    ALL_PARTICIPATING_FAMILIES,
)

__all__ = [
    "constants", "dataset", "engine", "strategies",
    "FEATURE_STORE", "CALL", "PUT", "FLAT", "THRESHOLD", "TARGET_THRESHOLD",
    "PRECISION_FLOOR", "MIN_FIRES", "WF_WINDOW", "WF_MIN_FIRES",
    "_VIX_COLS", "_BASE_STR_COLS", "_DROP_EXACT",
    "_label_at", "_call_ok", "_put_ok", "load_vix", "build_base", "scoring_frame",
    "Metrics", "score_signal", "_fmt",
    "gather_signals", "build_cascade", "walk_forward",
    "score_final", "_confusion_lines",
    "PROMOTED_FAMILIES", "PROMOTED_DEFINITIONS",
    "ALL_PARTICIPATING_FAMILIES",
]
