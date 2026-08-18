"""
NIFTY prediction-view library â€” unit tests.

Run:
    pytest tests/test_underlying_prediction.py
    python tests/test_underlying_prediction.py

These tests exercise the legacy prediction-view library
(src/technical_analysis/prediction: aggregator, expected_move, scoring, view,
schema) that the option-selection layer still depends on.

The production prediction pipeline (the precision cascade) now lives
in src/technical_analysis/cascade/pipeline.py; the daily job is
scripts/daily_NIFTY/daily_nifty_prediction.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from dotenv import load_dotenv

_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

load_dotenv(_repo_root / ".env")

# â”€â”€ prediction-view library (exercised by the unit tests below) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from src.technical_analysis.prediction.aggregator import build_strategy_signal, build_strategy_signals
from src.technical_analysis.prediction.expected_move import estimate_expected_move
from src.technical_analysis.prediction.schema import (
    RegimeSnapshot,
    StrategySignal,
    UnderlyingFeatureSnapshot,
)
from src.technical_analysis.prediction.scoring import score_underlying_view
from src.technical_analysis.prediction.view import build_underlying_view


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Unit tests  (pytest picks these up automatically)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _bullish_features() -> UnderlyingFeatureSnapshot:
    return UnderlyingFeatureSnapshot(
        symbol="TEST", trade_date="2026-05-15",
        close=110, volume=150000,
        ma10=108, ma20=105, ma50=100, ma90=95,
        ma20_slope=0.02, ma50_slope=0.015,
        rsi14=58, atr14=3,
        bb_upper=112, bb_middle=105, bb_lower=98, bb_width=0.12,
        ret_5d=0.03, ret_20d=0.08, ret_60d=0.18,
        volatility_20d=0.018, volume_avg_20d=100000,
        volume_ratio=None, trend_efficiency=0.55, range_position=0.75,
        relative_strength_vs_sector=0.03, relative_strength_vs_benchmark=0.04,
    )


def _bearish_features() -> UnderlyingFeatureSnapshot:
    return UnderlyingFeatureSnapshot(
        symbol="TEST", trade_date="2026-05-15",
        close=90, volume=150000,
        ma10=92, ma20=95, ma50=100, ma90=105,
        ma20_slope=-0.02, ma50_slope=-0.015,
        rsi14=42, atr14=3,
        bb_upper=102, bb_middle=95, bb_lower=88, bb_width=0.12,
        ret_5d=-0.03, ret_20d=-0.08, ret_60d=-0.18,
        volatility_20d=0.018, volume_avg_20d=100000,
        volume_ratio=None, trend_efficiency=0.55, range_position=0.25,
        relative_strength_vs_sector=-0.03, relative_strength_vs_benchmark=-0.04,
    )


def _regime(stock: str, sector: str | None = None, benchmark: str | None = None) -> RegimeSnapshot:
    return RegimeSnapshot(
        stock_regime=stock,  # type: ignore[arg-type]
        sector_regime=sector,  # type: ignore[arg-type]
        benchmark_regime=benchmark,  # type: ignore[arg-type]
        stock_regime_confidence=None, sector_regime_confidence=None,
        benchmark_regime_confidence=None, regime_reasons=[],
    )


class UnderlyingPredictionViewTests(unittest.TestCase):
    def test_bullish_trend_scores_high(self) -> None:
        score = score_underlying_view(
            _bullish_features(), _regime("TREND_UP", "TREND_UP", "TREND_UP"),
            "BULLISH", "TREND_UP_PULLBACK_LONG",
        )
        self.assertGreaterEqual(score.final_score, 80)

    def test_bearish_trend_scores_high(self) -> None:
        score = score_underlying_view(
            _bearish_features(), _regime("TREND_DOWN", "TREND_DOWN", "TREND_DOWN"),
            "BEARISH", "TREND_DOWN_RALLY_SHORT",
        )
        self.assertGreaterEqual(score.final_score, 80)

    def test_choppy_regime_blocks_option_eligibility(self) -> None:
        features = _bullish_features()
        reg = _regime("CHOPPY", "TREND_UP", "TREND_UP")
        signals = [build_strategy_signal("MaTrend_001", "CALL", features, reg)]
        view = build_underlying_view("TEST", "2026-05-15", features, reg, signals)
        self.assertFalse(view.is_option_eligible)
        self.assertEqual(view.option_bias, "NEUTRAL")

    def test_expected_move_uses_breakout_multiplier(self) -> None:
        expected_pct, expected_abs, holding_days = estimate_expected_move(
            _bullish_features(), "TREND_UP", "TREND_UP_BREAKOUT_LONG",
        )
        self.assertAlmostEqual(expected_abs or 0, 3.75)
        self.assertAlmostEqual(expected_pct or 0, 3.75 / 110)
        self.assertEqual(holding_days, 3)

    def test_underlying_view_maps_high_score_to_strong_bias(self) -> None:
        features = _bullish_features()
        reg = _regime("TREND_UP", "TREND_UP", "TREND_UP")
        signals = build_strategy_signals(
            {"MaTrend_001": "CALL", "trendUpRangeBreakout": "CALL"}, features, reg
        )
        view = build_underlying_view("TEST", "2026-05-15", features, reg, signals)
        self.assertEqual(view.raw_signal, "CALL")
        self.assertEqual(view.option_bias, "BULLISH_STRONG")
        self.assertTrue(view.is_option_eligible)

    def test_conflicting_strategies_return_no_position(self) -> None:
        features = _bullish_features()
        reg = _regime("TREND_UP", "TREND_UP", "TREND_UP")
        signals = [
            StrategySignal(
                strategy_name="bull", raw_signal="CALL", direction="BULLISH",
                setup_type="TREND_UP_PULLBACK_LONG", score=80, confidence="HIGH",
                expected_holding_days=3, expected_move_pct=0.02, expected_move_abs=2,
                stop_loss_pct=None, target_pct=0.02, reward_risk=None, reasons=[], warnings=[],
            ),
            StrategySignal(
                strategy_name="bear", raw_signal="PUT", direction="BEARISH",
                setup_type="TREND_DOWN_RALLY_SHORT", score=80, confidence="HIGH",
                expected_holding_days=3, expected_move_pct=0.02, expected_move_abs=2,
                stop_loss_pct=None, target_pct=0.02, reward_risk=None, reasons=[], warnings=[],
            ),
        ]
        view = build_underlying_view("TEST", "2026-05-15", features, reg, signals)
        self.assertEqual(view.raw_signal, "NO_POSITION")
        self.assertFalse(view.is_option_eligible)


if __name__ == "__main__":
    unittest.main()


