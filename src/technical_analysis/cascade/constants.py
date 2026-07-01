"""Shared constants for the NIFTY regime-aware precision cascade.

This package is the single source of truth for the cascade ENGINE and the
PROMOTED strategy roster. Two pipelines consume it:

  * backtest/vectorbt_research/strategy_grid.py  — research harness; registers the FULL
    strategy roster (promoted + still-experimental) and writes the leaderboard
    artifacts (strategy_grid_leaderboard.csv, strategy_grid_trades.csv, etc.).
  * src/technical_analysis/cascade/pipeline.py / scripts/daily_NIFTY â€” production;
    registers ONLY the promoted roster and emits the single final prediction.

The engine math (regime routing, labelling, precision-floor voting, scoring) is
identical for both; only the strategy roster differs, so the two pipelines can
diverge over time without the engine drifting.
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
    from src.common.config import get_regime_threshold
    return {REGIME_STRESS: get_regime_threshold(REGIME_STRESS), REGIME_CALM: get_regime_threshold(REGIME_CALM)}

REGIME_THRESHOLD = _build_regime_threshold()

# Production evaluation is deliberately stable across daily runs and UI filters.
PRODUCTION_BACKTEST_START = "2025-01-01"

# Columns dropped when forming the feature-only base.
_DROP_EXACT = {"final_raw_signal", "selected_regime", "hindsight_regime",
               "expected_regime_lag2", "actual_trade_label"}
_VIX_COLS = ["vix_close", "vix_chg_1d", "vix_chg_pct"]

# Columns held as strings (everything else in the base schema is numeric and is
# coerced with pd.to_numeric when freshly pulled from the DB).
_BASE_STR_COLS = {"trade_date", "next_trade_date", "final_prediction", "final_position"}

# â”€â”€ precision-cascade voting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PRECISION_FLOOR = 0.70   # default floor (stress regime); see REGIME_PRECISION_FLOOR
# A side (CALL/PUT) may only vote if its precision clears its regime's floor. The
# calm tape's edge is thinner (lower base rate at the 0.3% threshold), so calm
# uses a lower floor than the stressed tape.
REGIME_PRECISION_FLOOR = {REGIME_STRESS: 0.70, REGIME_CALM: 0.55}
MIN_FIRES = 5            # ...and it fired that side at least this many times (noise guard)
WF_WINDOW = 120          # trailing-day lookback for walk-forward eligibility
WF_MIN_FIRES = 4         # lighter fires guard inside the shorter walk-forward window
