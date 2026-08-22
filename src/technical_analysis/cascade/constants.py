"""Shared constants for the NIFTY precision cascade."""
from __future__ import annotations

from pathlib import Path


# Repo root: this file is src/technical_analysis/cascade/constants.py
project_root = Path(__file__).resolve().parents[3]

# Shared feature store path retained for callers that still export/read local artifacts.
FEATURE_STORE = project_root / "output" / "feature_store" / "NIFTY_base.csv"

# Side labels.
CALL, PUT, FLAT = "CALL", "PUT", "NO_POSITION"


def _build_target_threshold() -> float:
    from src.common.config import get_nifty_target_pct
    return get_nifty_target_pct()


# NIFTY underlying move threshold used for actual_trade_label.
THRESHOLD = _build_target_threshold()
TARGET_THRESHOLD = THRESHOLD

# Production evaluation is deliberately stable across daily runs and UI filters.
PRODUCTION_BACKTEST_START = "2024-01-01"

# Columns dropped when forming the feature-only base.
_DROP_EXACT = {
    "final_raw_signal",
    "actual_trade_label",
}
_VIX_COLS = ["vix_close", "vix_chg_1d", "vix_chg_pct"]

# Columns held as strings (everything else in the base schema is numeric and is
# coerced with pd.to_numeric when freshly pulled from the DB).
_BASE_STR_COLS = {"signal_date", "next_trade_date", "final_prediction", "final_position"}

# Research/audit precision thresholds.
PRECISION_FLOOR = 0.70
MIN_FIRES = 5
WF_WINDOW = 120
WF_MIN_FIRES = 4
COOLOFF_WINDOW = 5
