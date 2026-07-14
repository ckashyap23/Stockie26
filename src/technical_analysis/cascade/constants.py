"""Shared constants for the NIFTY regime-aware precision cascade.

This package is the single source of truth for the cascade ENGINE and the
PROMOTED strategy roster. Two pipelines consume it:

  * backtest/vectorbt_research/strategy_grid.py  — research harness; registers the FULL
    strategy roster (production-authorized + still-experimental) and writes the leaderboard
    artifacts (strategy_grid_leaderboard.csv, strategy_grid_trades.csv, etc.).
  * src/technical_analysis/cascade/pipeline.py / scripts/daily_NIFTY â€” production;
    registers only registry-authorized TRADE_ELIGIBLE/WATCH_ONLY strategies and
    emits the single final prediction plus watch promotions.

The engine math (regime routing, labelling, scoring) is shared; production
participation is controlled by strategy_families.yaml, not by an automatic
precision/fires gate.
"""
from __future__ import annotations

from pathlib import Path

# Repo root: this file is src/technical_analysis/cascade/constants.py
project_root = Path(__file__).resolve().parents[3]

# The canonical feature dataset (prices + features, point-in-time as of
# trade_date with realised next_* outcomes). Lives in a neutral, pipeline-agnostic
# location so both the research harness and production read/write the same store
# (it is NOT an experiment artifact). strategy_grid.py persists it; both
# pipelines read it as the feature store.
FEATURE_STORE = project_root / "output" / "feature_store" / "NIFTY_base.csv"

# Side labels.
CALL, PUT, FLAT = "CALL", "PUT", "NO_POSITION"

THRESHOLD = 0.005  # 0.5% next-day intraday move (touch) from next_open

# â”€â”€ volatility regime router â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# A same-day (point-in-time) split into a calm low-volatility tape and a
# stressed higher-volatility tape. Calm days rarely print a 0.5% intraday move,
# so they are graded against a smaller threshold; stressed days keep 0.5%.
REGIME_CALM, REGIME_STRESS = "calm", "stress"
REGIMES = (REGIME_STRESS, REGIME_CALM)
REGIME_VIX_CUTOFF = 13.0       # India VIX below this = calm
REGIME_VOL_CUTOFF = 0.007      # volatility_10d below this = calm

def _build_regime_threshold() -> dict[str, float]:
    from src.common.config import get_nifty_target_pct
    return {
        REGIME_STRESS: get_nifty_target_pct(REGIME_STRESS),
        REGIME_CALM: get_nifty_target_pct(REGIME_CALM),
    }

REGIME_THRESHOLD = _build_regime_threshold()

# Production evaluation is deliberately stable across daily runs and UI filters.
PRODUCTION_BACKTEST_START = "2024-01-01"

# Columns dropped when forming the feature-only base.
_DROP_EXACT = {"final_raw_signal", "selected_regime", "hindsight_regime",
               "expected_regime_lag2", "actual_trade_label"}
_VIX_COLS = ["vix_close", "vix_chg_1d", "vix_chg_pct"]

# Columns held as strings (everything else in the base schema is numeric and is
# coerced with pd.to_numeric when freshly pulled from the DB).
_BASE_STR_COLS = {"signal_date", "next_trade_date", "final_prediction", "final_position"}

# â”€â”€ research/audit precision thresholds â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PRECISION_FLOOR = 0.70   # default floor (stress regime); see REGIME_PRECISION_FLOOR
# These floors remain useful for research/UI audit, but production no longer
# gates registry-authorized strategies on computed precision/fires. The manual
# TRADE_ELIGIBLE/WATCH_ONLY tag is the production control point.
REGIME_PRECISION_FLOOR = {REGIME_STRESS: 0.70, REGIME_CALM: 0.55}
MIN_FIRES = 5
WF_WINDOW = 120
WF_MIN_FIRES = 4
