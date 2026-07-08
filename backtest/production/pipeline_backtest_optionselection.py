"""
NIFTY option selection — legacy research/E2E script (read-only, no DB upsert).

For production DB upserts use:
    python backtest/production/pipeline_upsert_option_selections.py

Script mode — read predictions from DB or CSV, run option selection, print P&L summary:
    python backtest/production/pipeline_backtest_optionselection.py --prediction-source db
    python backtest/production/pipeline_backtest_optionselection.py --prediction-source csv --input <path>

Unit tests:
    pytest tests/test_optionselection_e2e.py

P&L methodology:
    - as_of_time = signal_date 15:15:00 (EOD chain used for option selection)
    - Entry price = first OptionSnapshot price on the NEXT trading day
    - Optional premium-gap filter compares next-day entry vs signal-time reference
    - Targets are calculated from actual next-day entry price
    - Entry/exit uses the primary BUY leg of the selected strategy
    - Rows with is_option_eligible=False skip option selection (tagged NO_TRADE)
"""

from __future__ import annotations

import argparse
import sys
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
from dotenv import load_dotenv

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

load_dotenv(_repo_root / ".env")

# â”€â”€ unit test imports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from src.technical_analysis.optionselection.candidate_filter import (
    filter_long_call_candidates,
    filter_long_put_candidates,
)
from src.technical_analysis.optionselection.option_features import (
    compute_option_features_for_chain,
    compute_spread_pct,
    compute_theta_burn_pct_per_day,
)
from src.technical_analysis.optionselection.option_selector import select_option_strategy
from src.technical_analysis.optionselection.risk import calculate_strategy_risk
from src.technical_analysis.optionselection.schema import OptionContract, OptionSelectionResult
from src.technical_analysis.optionselection.scoring import score_option_candidate
from src.technical_analysis.optionselection.strategy_builder import build_strategy_candidates
from src.technical_analysis.optionselection.strategy_rules import choose_option_strategy_type
from src.technical_analysis.optionselection.underlying_view_strength import derive_option_bias
from src.technical_analysis.prediction.schema import UnderlyingView

# â”€â”€ pipeline imports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from src.common.config import get_settings, get_target_pct_for_regime, normalize_pct
from src.data_manager.db.client_factory import get_database_client


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Unit test helpers  (preserved from original test_optionselection.py)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def view(
    raw_signal: str = "CALL",
    direction: str = "BULLISH",
    score: float = 85,
    stock_regime: str = "TREND_UP",
) -> UnderlyingView:
    aligned_regime = "TREND_DOWN" if direction == "BEARISH" else "TREND_UP"
    return UnderlyingView(
        symbol="NIFTY",
        trade_date="2026-05-15",
        raw_signal=raw_signal,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        stock_regime=stock_regime,  # type: ignore[arg-type]
        sector_regime=aligned_regime,
        benchmark_regime=aligned_regime,
        primary_strategy="MaTrend_001",
        setup_type="TREND_UP_PULLBACK_LONG",
        strength_score=score,
        confidence="HIGH" if score >= 80 else "MEDIUM",
        expected_move_pct=0.012,
        expected_move_abs=120,
        expected_holding_days=3,
        atr14=100,
        volatility_20d=0.02,
        volume_ratio=1.2,
        relative_strength_vs_sector=0.02,
        relative_strength_vs_benchmark=0.02,
        stock_technical_score=25,
        sector_confirmation_score=15,
        benchmark_confirmation_score=10,
        relative_strength_score=15,
        volume_confirmation_score=7,
        risk_quality_score=10,
        regime_quality_score=15,
        strategy_signals=[],
        reasons=[],
        warnings=[],
        is_option_eligible=True,
        option_bias=(
            "BULLISH_STRONG" if direction == "BULLISH" and score >= 80
            else "BULLISH_MODERATE" if direction == "BULLISH"
            else "BEARISH_STRONG" if score >= 80
            else "BEARISH_MODERATE"
        ),
    )


def contract(
    symbol: str,
    strike: float,
    option_type: str,
    delta: float,
    expiry: str = "2026-05-29",
    bid: float = 99,
    ask: float = 101,
    last_price: float = 100,
    theta: float = -4,
    iv: float = 0.22,
) -> OptionContract:
    return OptionContract(
        instrument_token=1,
        tradingsymbol=symbol,
        underlying="NIFTY",
        expiry=expiry,
        strike=strike,
        option_type=option_type,  # type: ignore[arg-type]
        last_price=last_price,
        bid=bid,
        ask=ask,
        volume=1000,
        open_interest=5000,
        iv=iv,
        delta=delta,
        gamma=0.001,
        theta=theta,
        vega=5,
    )


class OptionSelectionTests(unittest.TestCase):
    def test_feature_calculations(self) -> None:
        self.assertAlmostEqual(compute_spread_pct(99, 101) or 0, 0.02)
        self.assertAlmostEqual(compute_theta_burn_pct_per_day(-4, 100) or 0, 0.04)

    def test_bias_and_strategy_rules(self) -> None:
        self.assertEqual(derive_option_bias(view()), "BULLISH_STRONG")
        self.assertEqual(derive_option_bias(view(score=60)), "NEUTRAL")
        self.assertEqual(derive_option_bias(view(stock_regime="CHOPPY")), "BULLISH_STRONG")
        self.assertEqual(
            choose_option_strategy_type("BULLISH_STRONG", 40, None, 0.01, 3, 10),
            "LONG_CALL",
        )
        self.assertEqual(
            choose_option_strategy_type("BULLISH_STRONG", 75, None, 0.01, 3, 10),
            "LONG_CALL",
        )
        self.assertEqual(
            choose_option_strategy_type("BULLISH_STRONG", 40, None, 0.01, 3, 1),
            "LONG_CALL",
        )
        self.assertEqual(
            choose_option_strategy_type("BEARISH_MODERATE", 40, None, 0.01, 3, 10),
            "LONG_PUT",
        )

    def test_long_candidate_filters(self) -> None:
        contracts = [
            contract("NIFTY26JUN9800CE", 9800, "CE", 0.78, expiry="2026-06-19"),
            contract("NIFTY26JUN10200PE", 10200, "PE", -0.78, expiry="2026-06-19"),
            contract("NIFTY26JUN10200CE", 10200, "CE", 0.78, expiry="2026-06-19"),
            contract("NIFTY26JUN9800PE", 9800, "PE", -0.78, expiry="2026-06-19"),
            contract("NIFTY26JUN9700CE", 9700, "CE", 0.62, expiry="2026-06-19"),
            contract("NIFTY26MAY9800CE", 9800, "CE", 0.78),
        ]
        features = compute_option_features_for_chain(contracts, 10000, "2026-05-15", [0.15, 0.20, 0.25])
        self.assertEqual(len(filter_long_call_candidates(contracts, features)), 1)
        self.assertEqual(len(filter_long_put_candidates(contracts, features)), 1)

    def test_iv_outlier_uses_same_expiry_atm_iv(self) -> None:
        contracts = [
            contract("NIFTY26MAY10000CE", 10000, "CE", 0.50, expiry="2026-05-16", iv=0.50),
            contract("NIFTY26JUN9800CE", 9800, "CE", 0.78, expiry="2026-06-19", iv=0.20),
            contract("NIFTY26JUN10000CE", 10000, "CE", 0.52, expiry="2026-06-19", iv=0.21),
        ]

        features = compute_option_features_for_chain(contracts, 10000, "2026-05-15", [0.15, 0.20, 0.25])
        target = features["NIFTY26JUN9800CE"]

        self.assertAlmostEqual(target.iv_vs_atm_pct or 0, -0.047619, places=5)
        self.assertFalse(target.is_iv_outlier)
        self.assertEqual(len(filter_long_call_candidates(contracts, features)), 1)

    def test_strategy_build_risk_and_score(self) -> None:
        contracts = [
            contract("NIFTY26MAY10000CE", 10000, "CE", 0.52),
            contract("NIFTY26MAY10200CE", 10200, "CE", 0.30, bid=39, ask=41, last_price=40),
        ]
        features = compute_option_features_for_chain(contracts, 10000, "2026-05-15", [0.15, 0.20, 0.25])
        candidates = build_strategy_candidates("BULL_CALL_SPREAD", contracts, features, view(), 10000)
        self.assertEqual(len(candidates), 1)
        risked = calculate_strategy_risk(candidates[0])
        self.assertGreater(risked.total_delta or 0, 0)
        scored = score_option_candidate(risked, view(), features)
        self.assertGreaterEqual(scored.score, 65)

    def test_selector_no_trade_for_weak_underlying(self) -> None:
        result = select_option_strategy(object(), view(score=60), 10000)
        self.assertEqual(result.selected_strategy.strategy_type, "NO_TRADE")
        self.assertEqual(result.no_trade_reason, "Underlying signal score below threshold")

    def test_selector_selects_long_call(self) -> None:
        contracts = [
            contract("NIFTY26JUN9800CE", 9800, "CE", 0.78, expiry="2026-06-19"),
            contract("NIFTY26JUN10200PE", 10200, "PE", -0.78, expiry="2026-06-19"),
        ]
        with patch(
            "src.technical_analysis.optionselection.option_selector.load_option_chain_with_calcs",
            return_value=contracts,
        ):
            result = select_option_strategy(object(), view(), 10000, atm_iv_history_90d=[0.10, 0.20, 0.40])
        self.assertEqual(result.selected_strategy.strategy_type, "LONG_CALL")
        self.assertIsNone(result.no_trade_reason)

    def test_selector_selects_long_put(self) -> None:
        contracts = [
            contract("NIFTY26JUN9800CE", 9800, "CE", 0.78, expiry="2026-06-19"),
            contract("NIFTY26JUN10200PE", 10200, "PE", -0.78, expiry="2026-06-19"),
        ]
        with patch(
            "src.technical_analysis.optionselection.option_selector.load_option_chain_with_calcs",
            return_value=contracts,
        ):
            result = select_option_strategy(
                object(),
                view(raw_signal="PUT", direction="BEARISH", stock_regime="TREND_DOWN"),
                10000,
                atm_iv_history_90d=[0.10, 0.20, 0.40],
            )
        self.assertEqual(result.selected_strategy.strategy_type, "LONG_PUT")
        self.assertIsNone(result.no_trade_reason)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# E2E pipeline helpers
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DEFAULT_INPUT = Path("output") / "backtest" / "NIFTY" / "production" / "NIFTY_prediction.csv"
DEFAULT_OUTPUT = Path("output") / "backtest" / "NIFTY" / "production" / "NIFTY_optionSelection.csv"

_PROFIT_TARGET_PCT = get_target_pct_for_regime("calm")
_DEFAULT_MAX_PREMIUM_GAP_PCT = 0.10


def _f(val: Any) -> float | None:
    try:
        if val is None or pd.isna(val):
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _reconstruct_view(row: dict[str, Any]) -> UnderlyingView:
    trade_date = str(row.get("date") or row.get("trade_date"))
    prediction_side = str(row.get("effective_prediction") or "NO_POSITION")
    if prediction_side == "BULLISH":
        prediction_side = "CALL"
    elif prediction_side == "BEARISH":
        prediction_side = "PUT"
    if prediction_side not in {"CALL", "PUT"}:
        prediction_side = "NO_POSITION"
    internal_direction = "BULLISH" if prediction_side == "CALL" else "BEARISH" if prediction_side == "PUT" else "NEUTRAL"
    strength_score = float(row.get("strength_score") or 0)
    confidence = str(row.get("confidence") or "")
    if confidence not in {"LOW", "MEDIUM", "HIGH"}:
        confidence = "HIGH" if strength_score >= 80 else "MEDIUM" if strength_score >= 65 else "LOW"
    return UnderlyingView(
        symbol="NIFTY",
        trade_date=trade_date,
        raw_signal=prediction_side,  # type: ignore[arg-type]
        direction=internal_direction,  # type: ignore[arg-type]
        stock_regime=str(row.get("stock_regime") or row.get("volatility_regime") or row.get("regime") or "UNKNOWN"),  # type: ignore[arg-type]
        sector_regime=None,
        benchmark_regime=None,
        primary_strategy=str(row.get("primary_strategy") or "") or None,
        setup_type=str(row.get("setup_type") or "NO_SETUP"),  # type: ignore[arg-type]
        strength_score=strength_score,
        confidence=confidence,  # type: ignore[arg-type]
        expected_move_pct=_f(row.get("expected_move_pct")),
        expected_move_abs=_f(row.get("expected_move_abs")),
        expected_holding_days=int(row.get("expected_holding_days") or (3 if prediction_side in {"CALL", "PUT"} else 0)),
        atr14=_f(row.get("atr14")),
        volatility_20d=_f(row.get("volatility_20d")),
        volume_ratio=None,
        relative_strength_vs_sector=_f(row.get("relative_strength_vs_sector")),
        relative_strength_vs_benchmark=None,
        stock_technical_score=float(row.get("stock_technical_score") or 0),
        sector_confirmation_score=float(row.get("sector_confirmation_score") or 0),
        benchmark_confirmation_score=float(row.get("benchmark_confirmation_score") or 0),
        relative_strength_score=float(row.get("relative_strength_score") or 0),
        volume_confirmation_score=float(row.get("volume_confirmation_score") or 0),
        risk_quality_score=float(row.get("risk_quality_score") or 0),
        regime_quality_score=float(row.get("regime_quality_score") or 0),
        strategy_signals=[],
        reasons=[],
        warnings=[],
        is_option_eligible=pd.NA,
        option_bias=str(row.get("option_bias") or "NEUTRAL"),  # type: ignore[arg-type]
    )


def _is_option_candidate_row(row: dict[str, Any]) -> bool:
    prediction_side = str(row.get("effective_prediction") or "NO_POSITION")
    strength_score = _f(row.get("strength_score")) or 0.0
    return prediction_side in {"CALL", "PUT"} and strength_score >= 65


def _fetch_atm_iv_history(conn, underlying: str, spot_price: float, as_of_date: date) -> list[float]:
    sql = """
        WITH ranked AS (
            SELECT
                os.trade_date,
                calc.implied_volatility,
                ROW_NUMBER() OVER (
                    PARTITION BY os.trade_date
                    ORDER BY ABS(oi.strike - %s), os.snapshot_time DESC, os.id DESC
                ) AS rn
            FROM "OptionSnapshot" os
            JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
            JOIN "OptionSnapshotCalc" calc ON calc.option_snapshot_id = os.id
            WHERE UPPER(oi.underlying) = %s
              AND os.trade_date <= %s
              AND calc.implied_volatility IS NOT NULL
              AND calc.implied_volatility > 0
        )
        SELECT implied_volatility FROM ranked WHERE rn = 1
        ORDER BY trade_date DESC
        LIMIT 90
    """
    with conn.cursor() as cur:
        cur.execute(sql, (spot_price, underlying.upper(), as_of_date))
        rows = cur.fetchall()
    return [float(r[0]) for r in rows if r[0] is not None]


def _load_prediction_rows_from_db(conn, underlying: str, model_version: str) -> pd.DataFrame:
    sql = """
        SELECT
            signal_date,
            next_trade_date,
            open_915,
            high_day,
            low_day,
            close_1515,
            volume_day,
            vix_close,
            vix_chg_1d,
            vix_chg_pct,
            regime,
            next_open,
            next_high,
            next_low,
            next_close,
            next_return_pct,
            final_prediction,
            promoted_prediction,
            effective_prediction,
            direction,
            volatility_regime,
            primary_strategy,
            strategy_precision,
            signal_style,
            strength_score,
            strength_label,
            confidence_level,
            actual_trade_label
        FROM "NiftyPrediction"
        WHERE UPPER(symbol) = %s
          AND model_version = %s
        ORDER BY signal_date
    """
    with conn.cursor() as cur:
        cur.execute(sql, (underlying.upper(), model_version))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    return pd.DataFrame(rows, columns=cols)


def _to_date(val: Any) -> date:
    return val if isinstance(val, date) else val.date() if hasattr(val, "date") else val


def _fetch_next_n_trading_days(conn, underlying: str, after_date: date, n: int | None = None) -> list[date]:
    if n is None:
        from src.common.config import get_trade_horizon_days
        n = get_trade_horizon_days()
    sql = """
        SELECT trade_date FROM "UnderlyingSnapshot"
        WHERE underlying = %s AND trade_date > %s
        ORDER BY trade_date
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (underlying.upper(), after_date, n))
        rows = cur.fetchall()
    return [_to_date(r[0]) for r in rows]


def _fetch_option_snapshots_for_dates(
    conn, instrument_token: int, trade_dates: list[date]
) -> pd.DataFrame:
    if not trade_dates:
        return pd.DataFrame(columns=["trade_date", "snapshot_time", "option_price", "lot_size"])
    sql = """
        SELECT os.trade_date, os.snapshot_time, os.last_price AS option_price, oi.lot_size
        FROM "OptionSnapshot" os
        JOIN "OptionInstrument" oi ON oi.id = os.option_instrument_id
        WHERE oi.instrument_token = %s
          AND os.trade_date IN %s
          AND os.last_price IS NOT NULL
          AND os.last_price > 0
        ORDER BY os.snapshot_time
    """
    with conn.cursor() as cur:
        cur.execute(sql, (instrument_token, tuple(trade_dates)))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    if not rows:
        return pd.DataFrame(columns=["trade_date", "snapshot_time", "option_price", "lot_size"])
    return pd.DataFrame(rows, columns=cols)


def _calculate_pnl(all_snaps: pd.DataFrame, next_day: date) -> dict[str, Any]:
    return _calculate_pnl_with_execution_rule(all_snaps, next_day)


def _calculate_pnl_with_execution_rule(
    all_snaps: pd.DataFrame,
    next_day: date,
    signal_entry_ref: float | None = None,
    max_premium_gap_pct: float | None = None,
    target_pct: float = _PROFIT_TARGET_PCT,
    gap_action: str = "skip",
) -> dict[str, Any]:
    """
    Entry = first snapshot on next_day (open proxy) unless a premium-gap rule is
    configured. If the open premium is above signal_ref * (1 + max_gap):
      - gap_action=skip: no trade.
      - gap_action=limit: scan next_day for the first snapshot at/below the max
        allowed entry and use that snapshot as entry.
    Exit = first target hit after entry on next_day, otherwise next_day close.
    """
    empty: dict[str, Any] = {
        "entry_price": None, "entry_time": None,
        "exit_price": None, "exit_time": None,
        "lot_size": None, "pnl_per_unit": None, "pnl_per_lot": None,
        "return_pct": None, "option_result": None,
        "target_pct": target_pct,
        "target_price": None,
        "target_hit_time": None,
        "exit_reason": None,
        "premium_gap_pct": None,
        "premium_gap_allowed": None,
        "entry_skip_reason": None,
        "first_2pct_profit_datetime": None,
    }
    if all_snaps.empty:
        return empty

    all_snaps = all_snaps.sort_values("snapshot_time").reset_index(drop=True)

    next_day_snaps = all_snaps[all_snaps["trade_date"].apply(
        lambda v: _to_date(v) == next_day
    )]
    if next_day_snaps.empty:
        return empty

    open_row = next_day_snaps.iloc[0]
    open_price = float(open_row["option_price"])
    entry_row = open_row

    premium_gap_pct: float | None = None
    premium_gap_allowed: bool | None = None
    max_allowed_entry: float | None = None
    if signal_entry_ref is not None and signal_entry_ref > 0 and max_premium_gap_pct is not None:
        premium_gap_pct = (open_price - signal_entry_ref) / signal_entry_ref
        max_allowed_entry = signal_entry_ref * (1 + max_premium_gap_pct)
        premium_gap_allowed = premium_gap_pct <= max_premium_gap_pct
        if not premium_gap_allowed:
            if gap_action == "skip":
                empty.update({
                    "entry_time": str(open_row["snapshot_time"])[:19],
                    "entry_price": round(open_price, 2),
                    "premium_gap_pct": round(premium_gap_pct * 100, 2),
                    "premium_gap_allowed": False,
                    "entry_skip_reason": (
                        f"Open premium gap {premium_gap_pct:.2%} exceeded "
                        f"max {max_premium_gap_pct:.2%}"
                    ),
                    "option_result": "SKIPPED",
                })
                return empty
            if gap_action == "limit":
                fillable = next_day_snaps[next_day_snaps["option_price"].astype(float) <= max_allowed_entry]
                if fillable.empty:
                    empty.update({
                        "entry_time": str(open_row["snapshot_time"])[:19],
                        "entry_price": round(open_price, 2),
                        "premium_gap_pct": round(premium_gap_pct * 100, 2),
                        "premium_gap_allowed": False,
                        "entry_skip_reason": (
                            f"Limit {max_allowed_entry:.2f} not reached after "
                            f"open gap {premium_gap_pct:.2%}"
                        ),
                        "option_result": "SKIPPED",
                    })
                    return empty
                entry_row = fillable.iloc[0]

    entry_price = float(entry_row["option_price"])
    if entry_price <= 0:
        return empty

    lot_size_raw = entry_row.get("lot_size")
    lot_size = int(lot_size_raw) if lot_size_raw is not None and not pd.isna(lot_size_raw) else None

    entry_time = entry_row["snapshot_time"]
    post_entry_snaps = next_day_snaps[next_day_snaps["snapshot_time"] >= entry_time]
    target_price = entry_price * (1 + target_pct)
    target_hits = post_entry_snaps[post_entry_snaps["option_price"].astype(float) >= target_price]
    if not target_hits.empty:
        exit_row = target_hits.iloc[0]
        exit_reason = "TARGET_HIT"
        target_hit_time = str(exit_row["snapshot_time"])[:19]
    else:
        exit_row = post_entry_snaps.iloc[-1]
        exit_reason = "EOD_MARK"
        target_hit_time = None

    exit_price = float(exit_row["option_price"])
    pnl_per_unit = exit_price - entry_price
    pnl_per_lot = pnl_per_unit * lot_size if lot_size else None
    return_pct = pnl_per_unit / entry_price

    return {
        "entry_time": str(entry_row["snapshot_time"])[:19],
        "entry_price": round(entry_price, 2),
        "exit_time": str(exit_row["snapshot_time"])[:19],
        "exit_price": round(exit_price, 2),
        "lot_size": lot_size,
        "pnl_per_unit": round(pnl_per_unit, 2),
        "pnl_per_lot": round(pnl_per_lot, 2) if pnl_per_lot is not None else None,
        "return_pct": round(return_pct * 100, 2),
        "option_result": "PROFIT" if pnl_per_unit > 0 else ("LOSS" if pnl_per_unit < 0 else "BREAKEVEN"),
        "target_pct": target_pct,
        "target_price": round(target_price, 2),
        "target_hit_time": target_hit_time,
        "exit_reason": exit_reason,
        "premium_gap_pct": round(premium_gap_pct * 100, 2) if premium_gap_pct is not None else None,
        "premium_gap_allowed": premium_gap_allowed,
        "entry_skip_reason": None,
        "first_2pct_profit_datetime": target_hit_time if abs(target_pct - 0.02) < 1e-9 else None,
    }


def _flatten_result(result: OptionSelectionResult) -> dict[str, Any]:
    cand = result.selected_strategy
    legs_summary = "; ".join(
        f"{leg.side} {leg.contract.tradingsymbol} @{leg.contract.last_price}"
        for leg in cand.legs
    ) if cand.legs else ""
    first_buy = next((leg for leg in cand.legs if leg.side == "BUY"), None)
    return {
        "selected_strategy": cand.strategy_type,
        "option_bias_selected": str(result.option_bias),
        "no_trade_reason": result.no_trade_reason or "",
        "evaluated_candidate_count": result.evaluated_candidate_count,
        "strategy_direction": cand.direction,
        "entry_debit_or_credit": cand.entry_debit_or_credit,
        "max_profit": cand.max_profit,
        "max_loss": cand.max_loss,
        "breakeven": cand.breakeven,
        "reward_risk": round(cand.reward_risk, 2) if cand.reward_risk else None,
        "selection_score": cand.score,
        "selection_confidence": str(cand.confidence),
        "total_delta": cand.total_delta,
        "total_theta": cand.total_theta,
        "total_vega": cand.total_vega,
        "legs_summary": legs_summary,
        "primary_buy_token": first_buy.contract.instrument_token if first_buy else None,
        "primary_buy_symbol": first_buy.contract.tradingsymbol if first_buy else None,
        "primary_buy_strike": first_buy.contract.strike if first_buy else None,
        "primary_buy_expiry": first_buy.contract.expiry if first_buy else None,
        "primary_buy_option_type": first_buy.contract.option_type if first_buy else None,
        "primary_buy_entry_price_signal": first_buy.contract.last_price if first_buy else None,
        "primary_buy_iv": first_buy.contract.iv if first_buy else None,
        "primary_buy_delta": first_buy.contract.delta if first_buy else None,
        "selection_reasons": " | ".join(cand.reasons),
        "selection_warnings": " | ".join(cand.warnings),
    }


def generate_option_selection_csv(
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    underlying: str = "NIFTY",
    prediction_source: str = "csv",
    model_version: str = "cascade_v1",
    max_premium_gap_pct: float | None = _DEFAULT_MAX_PREMIUM_GAP_PCT,
    gap_action: str = "skip",
    target_pct: float = _PROFIT_TARGET_PCT,
) -> dict[str, Any]:
    settings = get_settings()
    db = get_database_client(settings)
    db.connect()

    if prediction_source == "db":
        pred_df = _load_prediction_rows_from_db(db.conn, underlying, model_version)
        if pred_df.empty:
            db.close()
            print(f"No NiftyPrediction rows found for {underlying} model_version={model_version}.")
            return {"rows": 0, "path": str(output_path)}
        print(f"Loaded {len(pred_df)} prediction rows from NiftyPrediction")
    elif not input_path.exists():
        print(f"Prediction CSV missing at {input_path}; running underlying prediction first...")
        from src.technical_analysis.cascade.pipeline import generate_prediction_csv

        prediction_result = generate_prediction_csv(
            underlying=underlying.upper(),
            output_path=input_path,
            regime_comparison_path=(
                Path("output") / "backtest" / underlying.upper() / "regime" /
                f"{underlying.upper()}_regime_experiment_comparison.csv"
            ),
        )
        if int(prediction_result.get("rows") or 0) == 0 or not input_path.exists():
            db.close()
            raise FileNotFoundError(
                f"Prediction CSV not found at {input_path} and automatic generation produced no rows."
            )
        pred_df = pd.read_csv(input_path)
    else:
        pred_df = pd.read_csv(input_path)

    if pred_df.empty:
        db.close()
        print("Prediction CSV is empty â€” nothing to process.")
        return {"rows": 0, "path": str(output_path)}

    if prediction_source != "db":
        print(f"Loaded {len(pred_df)} prediction rows from {input_path}")

    output_rows: list[dict[str, Any]] = []
    try:
        for _, pred_row in pred_df.iterrows():
            base = pred_row.to_dict()
            signal_date_str = str(
                base.get("signal_date") or base.get("date") or base.get("trade_date")
            )
            signal_date = date.fromisoformat(signal_date_str)
            is_eligible = _is_option_candidate_row(base)

            if not is_eligible:
                base.update({
                    "selected_strategy": "NO_TRADE",
                    "option_bias_selected": base.get("option_bias", "NEUTRAL"),
                    "no_trade_reason": "No tradable direction/strength",
                    "evaluated_candidate_count": 0,
                    "strategy_direction": "", "entry_debit_or_credit": None,
                    "max_profit": None, "max_loss": None, "breakeven": None,
                    "reward_risk": None, "selection_score": 0, "selection_confidence": "",
                    "total_delta": None, "total_theta": None, "total_vega": None,
                    "legs_summary": "", "primary_buy_token": None, "primary_buy_symbol": None,
                    "primary_buy_strike": None, "primary_buy_expiry": None,
                    "primary_buy_option_type": None, "primary_buy_entry_price_signal": None,
                    "primary_buy_iv": None, "primary_buy_delta": None,
                    "selection_reasons": "", "selection_warnings": "",
                    "next_trading_day": None,
                    "entry_time": None, "entry_price": None,
                    "exit_time": None, "exit_price": None,
                    "lot_size": None, "pnl_per_unit": None, "pnl_per_lot": None,
                    "return_pct": None, "option_result": None,
                    "target_pct": target_pct, "target_price": None, "target_hit_time": None,
                    "exit_reason": None, "premium_gap_pct": None,
                    "premium_gap_allowed": None, "entry_skip_reason": None,
                    "first_2pct_profit_datetime": None,
                })
                output_rows.append(base)
                continue

            underlying_view = _reconstruct_view(base)
            spot_price = _f(base.get("close")) or _f(base.get("close_1515")) or 0.0
            as_of_time = f"{signal_date_str} 15:15:00"

            iv_history: list[float] = []
            if spot_price > 0:
                try:
                    iv_history = _fetch_atm_iv_history(db.conn, underlying, spot_price, signal_date)
                except Exception as exc:
                    print(f"  {signal_date_str}: IV history fetch failed â€” {exc}")

            try:
                result = select_option_strategy(db, underlying_view, spot_price, as_of_time, iv_history or None)
            except Exception as exc:
                print(f"  {signal_date_str}: option selection failed â€” {exc}")
                result = None

            flat: dict[str, Any] = {}
            if result is not None:
                flat = _flatten_result(result)
            else:
                flat = {
                    "selected_strategy": "NO_TRADE", "option_bias_selected": "",
                    "no_trade_reason": "Selection error", "evaluated_candidate_count": 0,
                    "strategy_direction": "", "entry_debit_or_credit": None,
                    "max_profit": None, "max_loss": None, "breakeven": None,
                    "reward_risk": None, "selection_score": 0, "selection_confidence": "",
                    "total_delta": None, "total_theta": None, "total_vega": None,
                    "legs_summary": "", "primary_buy_token": None, "primary_buy_symbol": None,
                    "primary_buy_strike": None, "primary_buy_expiry": None,
                    "primary_buy_option_type": None, "primary_buy_entry_price_signal": None,
                    "primary_buy_iv": None, "primary_buy_delta": None,
                    "selection_reasons": "", "selection_warnings": "",
                }

            # P&L: only if a real trade was selected
            pnl: dict[str, Any] = {
                "next_trading_day": None,
                "entry_time": None, "entry_price": None,
                "exit_time": None, "exit_price": None,
                "lot_size": None, "pnl_per_unit": None, "pnl_per_lot": None,
                "return_pct": None, "option_result": None,
                "target_pct": target_pct, "target_price": None, "target_hit_time": None,
                "exit_reason": None, "premium_gap_pct": None,
                "premium_gap_allowed": None, "entry_skip_reason": None,
                "first_2pct_profit_datetime": None,
            }
            buy_token = flat.get("primary_buy_token")
            if flat.get("selected_strategy") not in (None, "", "NO_TRADE") and buy_token:
                try:
                    trading_days = _fetch_next_n_trading_days(db.conn, underlying, signal_date)
                    next_day = trading_days[0] if trading_days else None
                    pnl["next_trading_day"] = str(next_day) if next_day else None
                    if next_day and trading_days:
                        snaps = _fetch_option_snapshots_for_dates(db.conn, int(buy_token), trading_days)
                        pnl.update(_calculate_pnl_with_execution_rule(
                            snaps,
                            next_day,
                            signal_entry_ref=_f(flat.get("primary_buy_entry_price_signal")),
                            max_premium_gap_pct=max_premium_gap_pct,
                            target_pct=target_pct,
                            gap_action=gap_action,
                        ))
                except Exception as exc:
                    print(f"  {signal_date_str}: P&L scan failed â€” {exc}")

            base.update(flat)
            base.update(pnl)
            output_rows.append(base)
            strategy = flat.get("selected_strategy", "NO_TRADE")
            target_hit = pnl.get("target_hit_time") or ""
            skip = pnl.get("entry_skip_reason") or ""
            print(
                f"  {signal_date_str}: {strategy} â€” pnl={pnl.get('pnl_per_unit')!s:>8}  "
                f"target@{target_hit or 'N/A'}"
                + (f"  SKIP: {skip}" if skip else "")
            )

    finally:
        db.close()

    if not output_rows:
        print("No rows produced.")
        return {"rows": 0, "path": str(output_path)}

    out_df = pd.DataFrame(output_rows)
    print(f"\nProcessed {len(out_df)} rows")

    eligible_mask = out_df.apply(lambda row: _is_option_candidate_row(row.to_dict()), axis=1)
    eligible = out_df[eligible_mask]
    traded = eligible[eligible["selected_strategy"].notna() & (eligible["selected_strategy"] != "NO_TRADE")]
    if not traded.empty:
        wins = (traded["option_result"] == "PROFIT").sum()
        losses = (traded["option_result"] == "LOSS").sum()
        avg_ret = traded["return_pct"].mean()
        print(f"Backtest summary: {len(traded)} trades â€” W:{wins} L:{losses} â€” avg return: {avg_ret:.2f}%")

    return {"rows": len(out_df), "path": str(output_path)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run NIFTY option selection E2E and compute P&L backtest."
    )
    parser.add_argument(
        "--input", default=str(DEFAULT_INPUT),
        help=f"Prediction CSV from the cascade pipeline. Default: {DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output", default=str(DEFAULT_OUTPUT),
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument(
        "--prediction-source", choices=("csv", "db"), default="csv",
        help="Read upstream predictions from local CSV or Supabase NiftyPrediction. Default: csv",
    )
    parser.add_argument(
        "--model-version", default="cascade_v1",
        help="NiftyPrediction model_version when --prediction-source=db. Default: cascade_v1",
    )
    parser.add_argument(
        "--max-premium-gap-pct",
        type=float,
        default=_DEFAULT_MAX_PREMIUM_GAP_PCT,
        help="Skip/limit entries when next-day open premium exceeds signal reference by this decimal pct. Default: 0.10",
    )
    parser.add_argument(
        "--no-premium-gap-filter",
        action="store_true",
        help="Disable the premium-gap entry filter.",
    )
    parser.add_argument(
        "--gap-action",
        choices=("skip", "limit"),
        default="skip",
        help="When premium gap is too high: skip the trade or wait for a limit fill. Default: skip",
    )
    parser.add_argument(
        "--target-pct",
        type=float,
        default=_PROFIT_TARGET_PCT,
        help="Single profit target, calculated from actual entry price. Default: CALM_TARGET_PCT.",
    )
    args = parser.parse_args()

    result = generate_option_selection_csv(
        input_path=Path(args.input),
        output_path=Path(args.output),
        underlying=args.underlying.upper(),
        prediction_source=args.prediction_source,
        model_version=args.model_version,
        max_premium_gap_pct=None if args.no_premium_gap_filter else args.max_premium_gap_pct,
        gap_action=args.gap_action,
        target_pct=normalize_pct(args.target_pct),
    )
    print(result)


if __name__ == "__main__":
    main()

