"""Export in-sample precision misses and recall misses with signal-day features.

Outputs
-------
NIFTY_{regime}_in_sample_precision_misses.csv
    Rows where we predicted CALL/PUT but the underlying did not reach the regime
    target — labelled with why_predicted, why_missed_category, why_missed.

NIFTY_{regime}_in_sample_recall_misses.csv
    Rows where actual_trade_label = CALL or PUT but we predicted NO_POSITION —
    labelled with why_not_predicted (per-promoted-strategy diagnosis) and
    why_missed (closest near-miss suggestion).
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from dotenv import load_dotenv
load_dotenv(_repo_root / ".env")

import numpy as np
import pandas as pd
from psycopg2.extras import RealDictCursor

from src.common.config import get_nifty_target_pct, get_settings
from src.technical_analysis.cascade.dataset import build_base


DEFAULT_INPUT = Path("output/backtest/NIFTY/production/NIFTY_prediction.csv")
DEFAULT_PRECISION_OUTPUT = Path(
    "output/backtest/NIFTY/production/NIFTY_stress_in_sample_precision_misses.csv"
)
DEFAULT_RECALL_OUTPUT = Path(
    "output/backtest/NIFTY/production/NIFTY_stress_in_sample_recall_misses.csv"
)

_PROMOTION_COLUMNS = [
    "watch_signal",
    "prior_watch_signal",
    "prior_watch_age",
    "promoted_prediction",
    "effective_prediction",
    "promotion_reason",
    "watch_family", "watch_variant", "watch_strategy_type",
    "prior_watch_family", "prior_watch_variant", "prior_watch_strategy_type",
    "confirming_family", "confirming_variant", "confirming_strategy_type",
    "family_confirmation_match",
    "promotion_block_reason", "primary_strategy_family",
    "primary_strategy_type",
]

# ── Strategy condition specs ──────────────────────────────────────────────────
# Each entry: (feature_col, operator, threshold, fail_template, pass_template)
# operator: "<=", ">=", ">", "<"

_STRESS_CALL_STRATEGIES: dict[str, list[tuple]] = {
    "OversoldBounceCall_HighPrecision": [
        ("range_position_10d", "<=", 0.20,
         "range_position_10d={v:.3f} > 0.20 (not near 10-day low)",
         "near 10-day low ✓"),
        ("vix_close", ">=", 12.0,
         "vix_close={v:.1f} < 12 (low-volatility day)",
         "VIX ✓"),
    ],
    "OversoldBounceCall_MoreTrades": [
        ("rsi14", "<=", 42.0,
         "rsi14={v:.1f} > 42 (not oversold)",
         "RSI oversold ✓"),
        ("resistance_distance_10d", ">=", 0.025,
         "resistance_distance_10d={v:.3f} < 2.5% (tight headroom to resistance)",
         "resistance room ✓"),
        ("vix_close", ">=", 12.0,
         "vix_close={v:.1f} < 12",
         "VIX ✓"),
    ],
    "OversoldBounceCall_Guarded": [
        ("rsi14", "<=", 42.0,
         "rsi14={v:.1f} > 42 (not oversold)",
         "RSI oversold ✓"),
        ("resistance_distance_10d", ">=", 0.025,
         "resistance_distance_10d={v:.3f} < 2.5%",
         "resistance room ✓"),
        ("vix_close", ">=", 12.0,
         "vix_close={v:.1f} < 12",
         "VIX ✓"),
        ("ma20_slope", ">=", -0.01,
         "ma20_slope={v:.4f} < -0.01 (broad trend in strong down-leg)",
         "MA20 slope ✓"),
        ("ma5d_slope", ">=", -0.02,
         "ma5d_slope={v:.4f} < -0.02 (short slope in capitulation)",
         "MA5 slope ✓"),
    ],
    "MomentumDirectional_CALL": [
        # voting system: need >= 2 of these 4
        ("rsi14", "<=", 42.0, "rsi14={v:.1f} > 42", "RSI vote ✓"),
        ("ret_5d", "<", -0.012, "ret_5d={v:.3f} >= -1.2%", "ret_5d vote ✓"),
        ("resistance_distance_10d", ">=", 0.025, "resistance_distance_10d={v:.3f} < 2.5%", "room vote ✓"),
        ("range_position_10d", "<=", 0.25, "range_position_10d={v:.3f} > 0.25", "range vote ✓"),
    ],
    "BollingerMeanReversion_CALL": [
        ("vix_close", ">=", 12.0, "vix_close={v:.1f} < 12", "VIX ✓"),
        ("bb_lower", "is_below_close", None,
         "close={close:.1f} NOT below bb_lower={v:.1f} (not at lower band)",
         "at lower band ✓"),
    ],
}

_STRESS_PUT_STRATEGIES: dict[str, list[tuple]] = {
    "DownMomentumPut_HighPrecision": [
        ("ma20_slope", "<=", -0.003,
         "ma20_slope={v:.4f} > -0.003 (trend not falling)",
         "falling MA20 ✓"),
        ("volume_hybrid", ">=", 1.0,
         "volume={vol:.0f} < min(90k, 1.2×vol_20d={floor:.0f}) (low volume)",
         "volume ✓"),
        ("vix_chg_1d", ">", 0.0,
         "vix_chg_1d={v:.2f} <= 0 (VIX not rising)",
         "VIX rising ✓"),
    ],
    "DownMomentumPut_MoreTrades": [
        ("ma20_slope", "<=", -0.003,
         "ma20_slope={v:.4f} > -0.003 (trend not falling)",
         "falling MA20 ✓"),
        ("volume_hybrid", ">=", 1.0,
         "volume={vol:.0f} < min(90k, 1.2×vol_20d={floor:.0f}) (low volume)",
         "volume ✓"),
        ("vix_close", ">=", 12.0,
         "vix_close={v:.1f} < 12",
         "VIX level ✓"),
    ],
    "MomentumDirectional_PUT": [
        # voting system: need >= 3 of these 5
        ("ma_slope_combo", "<=", -0.003, "ma20_slope={v:.4f} AND ma10d not ≤-0.4% (trend not falling)", "MA slope vote ✓"),
        ("ret_10d", "<=", -0.005, "ret_10d={v:.3f} >= -0.5%", "ret_10d vote ✓"),
        ("volume_day", ">=", 88000, "volume_day={v:.0f} < 88k", "volume vote ✓"),
        ("bb_width", ">=", 0.055, "bb_width={v:.4f} < 5.5%", "BB width vote ✓"),
        ("range_position_10d", "<=", 0.40, "range_position_10d={v:.3f} > 0.40", "range vote ✓"),
    ],
    "BollingerMeanReversion_PUT": [
        ("vix_close", ">=", 12.0, "vix_close={v:.1f} < 12", "VIX ✓"),
        ("bb_upper", "is_above_close", None,
         "close={close:.1f} NOT above bb_upper={v:.1f} (not at upper band)",
         "at upper band ✓"),
    ],
}

_CALM_CALL_STRATEGIES: dict[str, list[tuple]] = {
    "CalmTrendCall_Headroom": [
        ("bb_width", ">=", 0.040, "bb_width={v:.4f} < 4%", "BB width ✓"),
        ("ma20_slope", ">", 0.0, "ma20_slope={v:.4f} <= 0 (no uptrend)", "uptrend ✓"),
        ("resistance_distance_10d", ">=", 0.015, "resistance_distance_10d={v:.3f} < 1.5%", "headroom ✓"),
        ("ma10d_slope", "<=", 0.0, "ma10d_slope={v:.4f} > 0 (no dip: short slope still rising)", "dip ✓"),
    ],
    "CalmTrendCall_Pullback": [
        ("bb_width", ">=", 0.040, "bb_width={v:.4f} < 4%", "BB width ✓"),
        ("ma20_slope", ">", 0.0, "ma20_slope={v:.4f} <= 0 (no uptrend)", "uptrend ✓"),
        ("range_position_10d", "<=", 0.5, "range_position_10d={v:.3f} > 0.50 (not a dip)", "pullback ✓"),
        ("trend_efficiency_10d", ">=", 0.25, "trend_efficiency_10d={v:.3f} < 0.25 (choppy trend)", "efficiency ✓"),
    ],
    "CalmMomentumCall_Continuation": [
        ("bb_width", ">=", 0.040, "bb_width={v:.4f} < 4%", "BB width ✓"),
        ("ret_3d", ">=", 0.003, "ret_3d={v:.4f} < 0.3%", "3d rise ✓"),
        ("ma5d_slope", ">", 0.0, "ma5d_slope={v:.4f} <= 0", "MA5 slope ✓"),
        ("ma10d_slope", ">=", -0.001, "ma10d_slope={v:.4f} < -0.1%", "MA10 slope ✓"),
        ("range_position_10d", "<=", 0.95, "range_position_10d={v:.3f} > 0.95", "range room ✓"),
    ],
}

_CALM_PUT_STRATEGIES: dict[str, list[tuple]] = {
    "CalmFadePut_Overbought": [
        ("bb_width", ">=", 0.040, "bb_width={v:.4f} < 4%", "BB width ✓"),
        ("rsi14", ">=", 65.0, "rsi14={v:.1f} < 65 (not overbought)", "RSI overbought ✓"),
        ("rsi5", ">=", 80.0, "rsi5={v:.1f} < 80 (not short-term exhausted)", "RSI5 exhausted ✓"),
    ],
    "CalmMomentumPut_Continuation": [
        ("bb_width", ">=", 0.040, "bb_width={v:.4f} < 4%", "BB width ✓"),
        ("ret_3d", "<=", -0.003, "ret_3d={v:.4f} > -0.3% (no 3-day decline)", "3d decline ✓"),
        ("ma5d_slope", "<", 0.0, "ma5d_slope={v:.4f} >= 0", "MA5 slope ✓"),
        ("range_position_10d", ">=", 0.20, "range_position_10d={v:.3f} < 0.20", "range floor ✓"),
    ],
}


def _check_condition(row: pd.Series, col: str, op: str, threshold, fail_tmpl: str, pass_tmpl: str) -> tuple[bool, str]:
    """Return (passed, description)."""
    if col == "volume_hybrid":
        vol = row.get("volume_day", float("nan"))
        vol_20d = row.get("volume_20d", float("nan"))
        if pd.isna(vol) or pd.isna(vol_20d):
            return True, "volume_hybrid=n/a (missing component; neutral fallback)"
        floor = min(90_000, 1.2 * vol_20d)
        passed = vol >= floor
        msg = pass_tmpl if passed else fail_tmpl.format(vol=vol, floor=floor)
        return passed, msg

    if col == "ma_slope_combo":
        slopes = [row.get(name) for name in ("ma5d_slope", "ma10d_slope", "ma20_slope")]
        if any(value is None or pd.isna(value) for value in slopes):
            return False, "ma_slope_combo=n/a (missing slope component)"
        combo = 0.50 * slopes[0] + 0.30 * slopes[1] + 0.20 * slopes[2]
        passed = combo <= threshold
        msg = pass_tmpl if passed else fail_tmpl.format(v=combo)
        return passed, msg

    v = row.get(col)
    if v is None or pd.isna(v):
        return False, f"{col}=n/a (missing feature)"

    if op == "is_below_close":
        close = row.get("close_1515", float("nan"))
        passed = close < v
        msg = pass_tmpl if passed else fail_tmpl.format(v=v, close=close)
        return passed, msg

    if op == "is_above_close":
        close = row.get("close_1515", float("nan"))
        passed = close > v
        msg = pass_tmpl if passed else fail_tmpl.format(v=v, close=close)
        return passed, msg

    v_float = float(v)
    if op == "<=":
        passed = v_float <= threshold
    elif op == ">=":
        passed = v_float >= threshold
    elif op == ">":
        passed = v_float > threshold
    elif op == "<":
        passed = v_float < threshold
    else:
        return False, f"unknown op {op}"

    msg = pass_tmpl if passed else fail_tmpl.format(v=v_float)
    return passed, msg


def _diagnose_strategy(row: pd.Series, strategy_name: str, conditions: list[tuple], is_voting: bool = False) -> tuple[bool, str]:
    """Return (would_fire, diagnosis_text)."""
    results = []
    for cond in conditions:
        col, op, threshold, fail_tmpl, pass_tmpl = cond
        passed, desc = _check_condition(row, col, op, threshold, fail_tmpl, pass_tmpl)
        results.append((passed, desc))

    passes = [r[0] for r in results]
    fails = [r[1] for r in results if not r[0]]

    if is_voting:
        n_pass = sum(passes)
        required = 2 if "CALL" in strategy_name else 3
        would_fire = n_pass >= required
        if would_fire:
            return True, f"would have fired ({n_pass}/{len(conditions)} votes, needed {required})"
        else:
            return False, f"only {n_pass}/{len(conditions)} votes (needed {required}): {'; '.join(fails)}"
    else:
        would_fire = all(passes)
        if would_fire:
            return True, "all conditions met — would have fired"
        return False, "; ".join(fails)


def _why_not_predicted(row: pd.Series, actual_direction: str, regime: str) -> str:
    if regime == "stress":
        call_strats = _STRESS_CALL_STRATEGIES
        put_strats = _STRESS_PUT_STRATEGIES
    else:
        call_strats = _CALM_CALL_STRATEGIES
        put_strats = _CALM_PUT_STRATEGIES

    strats = call_strats if actual_direction == "CALL" else put_strats
    voting_names = {"MomentumDirectional_CALL", "MomentumDirectional_PUT"}
    parts = []
    for name, conditions in strats.items():
        is_voting = name in voting_names
        _, diagnosis = _diagnose_strategy(row, name, conditions, is_voting)
        parts.append(f"[{name}] {diagnosis}")

    if row.get("global_risk_off") == "YES":
        parts.insert(0, "[GLOBAL_GATE] Global risk-off gate was active — all strategies blocked")

    return " | ".join(parts)


def _why_missed_recall(row: pd.Series, actual_direction: str, regime: str) -> str:
    """Suggest the closest near-miss improvement."""
    suggestions = []

    if row.get("global_risk_off") == "YES":
        suggestions.append("Global gate blocked entry; underlying moved despite risk-off signal — consider relaxing global gate for high-conviction days")

    if actual_direction == "CALL":
        rsi = row.get("rsi14")
        rp10 = row.get("range_position_10d")
        ret5 = row.get("ret_5d")
        room = row.get("resistance_distance_10d")

        if rsi is not None and not np.isnan(rsi):
            if rsi <= 55:
                suggestions.append(f"RSI14={rsi:.1f} — a looser oversold threshold (e.g. <=50) would include this")
        if rp10 is not None and not np.isnan(rp10):
            if 0.20 < rp10 <= 0.35:
                suggestions.append(f"range_position_10d={rp10:.3f} just above 0.20 floor — HighPrecision threshold of 0.30 would catch this")
        if ret5 is not None and not np.isnan(ret5) and -0.02 < ret5 < -0.008:
            suggestions.append(f"ret_5d={ret5:.3f} near -1.2% vote threshold — MomentumDirectional would have fired with one more vote")
        if room is not None and not np.isnan(room) and 0.015 <= room < 0.025:
            suggestions.append(f"resistance_distance_10d={room:.3f} just below 2.5% — lowering room floor to 1.5% catches this")
        if not suggestions:
            suggestions.append("No promoted CALL strategy fired — consider a new calm-tape rebound setup for this feature profile")

    else:  # PUT
        ma20 = row.get("ma20_slope")
        vol = row.get("volume_day")
        vol_20d = row.get("volume_20d")
        vix_chg = row.get("vix_chg_1d")
        bbw = row.get("bb_width")
        ret10 = row.get("ret_10d")

        if ma20 is not None and not np.isnan(ma20) and -0.003 < ma20 <= 0.0:
            suggestions.append(f"ma20_slope={ma20:.4f} just above -0.003 floor — DownMomentum threshold of -0.001 would catch flat-to-down trends")
        if vix_chg is not None and not np.isnan(vix_chg) and vix_chg == 0.0:
            suggestions.append("vix_chg_1d=0 (VIX unchanged) — MoreTrades variant (VIX level >= 12) would have been eligible")
        if vol is not None and vol_20d is not None and not np.isnan(vol) and not np.isnan(vol_20d):
            floor = min(90_000, 1.2 * vol_20d)
            if 0.85 * floor <= vol < floor:
                suggestions.append(f"volume={vol:.0f} just below floor={floor:.0f} (within 15%) — small volume relaxation catches this")
        if bbw is not None and not np.isnan(bbw) and 0.04 <= bbw < 0.055:
            suggestions.append(f"bb_width={bbw:.4f} below 5.5% MomentumDirectional gate — market not expanded enough for momentum PUT")
        if ret10 is not None and not np.isnan(ret10) and -0.005 < ret10 <= 0.0:
            suggestions.append(f"ret_10d={ret10:.3f} near -0.5% threshold — one vote short for MomentumDirectional PUT")
        if not suggestions:
            suggestions.append("No promoted PUT strategy fired — consider a new breakdown/continuation setup for this feature profile")

    return "; ".join(suggestions) if suggestions else "No near-miss identified — feature profile too far from any promoted strategy threshold"


# ── Feature helpers ────────────────────────────────────────────────────────────

def _feature_rows(dates: list[date], symbol: str) -> pd.DataFrame:
    import psycopg2

    settings = get_settings()
    if not settings.supabase_conn_str:
        raise RuntimeError("SUPABASE_CONN_STR is required")
    sql = """
        SELECT sfd.*, mf.india_vix AS vix_close
        FROM "SignalFeatureDaily" sfd
        LEFT JOIN "MacroFactorDaily" mf
          ON mf.factor_date = sfd.signal_date AND mf.india_vix IS NOT NULL
        WHERE UPPER(sfd.symbol) = %s
          AND sfd.feature_version = 'v1'
          AND sfd.signal_date = ANY(%s)
        ORDER BY sfd.signal_date
    """
    with psycopg2.connect(settings.supabase_conn_str) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol.upper(), dates))
            return pd.DataFrame([dict(row) for row in cur.fetchall()])


def _predictions_with_promotions(input_path: Path, symbol: str) -> pd.DataFrame:
    """Load current predictions from the DB, with the CSV as offline fallback."""
    import psycopg2

    settings = get_settings()
    if not settings.supabase_conn_str:
        return pd.read_csv(input_path)
    sql = '''
        SELECT *
        FROM "NiftyPrediction"
        WHERE UPPER(symbol) = %s AND model_version = 'cascade_v1'
        ORDER BY signal_date
    '''
    with psycopg2.connect(settings.supabase_conn_str) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (symbol.upper(),))
            predictions = pd.DataFrame([dict(row) for row in cur.fetchall()])

    if predictions.empty:
        return pd.read_csv(input_path)

    predictions["signal_date"] = predictions["signal_date"].astype(str)
    predictions["effective_prediction"] = predictions["effective_prediction"].fillna(
        predictions["final_prediction"]
    )
    return predictions


def _prefix_features(features: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if features.empty:
        return pd.DataFrame(columns=[f"{prefix}lookup_date"])
    drop = [c for c in ("feature_id", "symbol", "feature_version") if c in features]
    out = features.drop(columns=drop).copy()
    out = out.rename(columns={c: f"{prefix}{c}" for c in out.columns})
    return out.rename(columns={f"{prefix}signal_date": f"{prefix}lookup_date"})


# ── Precision miss helpers (unchanged) ────────────────────────────────────────

def _why_predicted(row: pd.Series) -> str:
    if row.get("effective_prediction") != row.get("final_prediction"):
        return f"Watch promotion fired: {row.get('promotion_reason') or 'confirmation recorded'}"
    strategy = str(row.get("primary_strategy") or "")
    if "BollingerMeanReversion" in strategy:
        return (
            f"Bollinger fade fired; RSI14={row.get('signal_feature__rsi14'):.2f}, "
            f"RSI5={row.get('signal_feature__rsi5'):.2f}, "
            f"BB width={100 * row.get('signal_feature__bb_width'):.2f}%."
        )
    if "MomentumDirectional" in strategy:
        return (
            f"Momentum expansion fired; VIX={row.get('vix_close'):.2f}, "
            f"BB width={100 * row.get('signal_feature__bb_width'):.2f}%, "
            f"ret_5d={100 * row.get('signal_feature__ret_5d'):.2f}%, "
            f"ret_10d={100 * row.get('signal_feature__ret_10d'):.2f}%."
        )
    return f"{strategy} was the highest-precision eligible strategy firing that side."


def _miss_reason(row: pd.Series, threshold: float) -> tuple[str, str]:
    up = float(row["up_excursion_pct"])
    down = float(row["down_excursion_pct"])
    gap = (float(row["next_open"]) / float(row["close_1515"]) - 1.0) * 100
    target = threshold * 100
    actual = row["actual_trade_label"]
    prediction = row["effective_prediction"]
    if actual == "NO_POSITION":
        category = "TARGET_NOT_REACHED"
        detail = (
            f"Neither side reached the {target:.2f}% stress target over the configured "
            f"horizon (up {up:.2f}%, down {down:.2f}%)."
        )
    elif prediction == "CALL" and gap < 0:
        category = "OVERNIGHT_RISK_REVERSAL"
        detail = (
            f"The CALL faced a {gap:.2f}% opening gap and reversed: "
            f"up excursion {up:.2f}% versus down excursion {down:.2f}%."
        )
    elif max(up, down) >= target and min(up, down) >= target * 0.80:
        category = "WHIPSAW_NEAR_THRESHOLD"
        detail = (
            f"Both directions expanded; the predicted side approached the threshold "
            f"but the opposite side won (up {up:.2f}%, down {down:.2f}%)."
        )
    elif prediction == "CALL" and gap > 0:
        category = "BULLISH_GAP_FAILED"
        detail = (
            f"A {gap:.2f}% bullish opening gap failed; up excursion was {up:.2f}% "
            f"and downside reached {down:.2f}%."
        )
    else:
        category = "WRONG_WAY_REVERSAL"
        detail = f"The opposite side reached target (up {up:.2f}%, down {down:.2f}%)."
    return category, detail


# ── Precision miss generator ───────────────────────────────────────────────────

def generate(input_path: Path, output_path: Path, symbol: str, regime: str) -> pd.DataFrame:
    predictions = _predictions_with_promotions(input_path, symbol)
    predictions = predictions[predictions["next_open"].notna()].copy()
    fired = predictions[
        predictions["regime"].eq(regime)
        & predictions["effective_prediction"].isin(["CALL", "PUT"])
    ].copy()
    correct = (
        fired["effective_prediction"].eq("CALL")
        & fired["actual_trade_label"].isin(["CALL", "BOTH"])
    ) | (
        fired["effective_prediction"].eq("PUT")
        & fired["actual_trade_label"].isin(["PUT", "BOTH"])
    )
    misses = fired.loc[~correct].copy()

    base = build_base()
    outcomes = base[["signal_date", "future_high_nd", "future_low_nd"]].copy()
    outcomes["signal_date"] = outcomes["signal_date"].astype(str)
    misses = misses.merge(outcomes, on="signal_date", how="left")
    misses["up_excursion_pct"] = (
        (misses["future_high_nd"] - misses["next_open"]) / misses["next_open"] * 100
    )
    misses["down_excursion_pct"] = (
        (misses["next_open"] - misses["future_low_nd"]) / misses["next_open"] * 100
    )

    signal_dates = pd.to_datetime(misses["signal_date"]).dt.date
    trade_dates = pd.to_datetime(misses["next_trade_date"]).dt.date
    features = _feature_rows(sorted(set(signal_dates) | set(trade_dates)), symbol)
    signal_features = _prefix_features(features, "signal_feature__")
    next_features = _prefix_features(features, "next_trade_feature__")
    misses["_signal_lookup"] = signal_dates
    misses["_next_lookup"] = trade_dates
    misses = misses.merge(
        signal_features, left_on="_signal_lookup", right_on="signal_feature__lookup_date", how="left"
    ).merge(
        next_features, left_on="_next_lookup", right_on="next_trade_feature__lookup_date", how="left"
    )

    threshold = get_nifty_target_pct(regime)
    reasons = misses.apply(lambda row: _miss_reason(row, threshold), axis=1)
    misses["why_predicted"] = misses.apply(_why_predicted, axis=1)
    misses["why_missed_category"] = [reason[0] for reason in reasons]
    misses["why_missed"] = [reason[1] for reason in reasons]
    misses = misses.drop(columns=["_signal_lookup", "_next_lookup"])

    front = [
        "signal_date", "next_trade_date", "regime", "final_prediction",
        "watch_signal", "promoted_prediction", "effective_prediction", "promotion_reason",
        "primary_strategy_family", "primary_strategy_type",
        "watch_family", "watch_variant", "watch_strategy_type",
        "confirming_family", "confirming_variant", "confirming_strategy_type",
        "family_confirmation_match", "promotion_block_reason",
        "actual_trade_label", "primary_strategy", "strategy_precision",
        "strength_score", "up_excursion_pct", "down_excursion_pct",
        "why_predicted", "why_missed_category", "why_missed",
    ]
    misses = misses[front + [c for c in misses.columns if c not in front]]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    misses.to_csv(output_path, index=False)
    return misses


# ── Recall miss generator ─────────────────────────────────────────────────────

def generate_recall_misses(input_path: Path, output_path: Path, symbol: str, regime: str) -> pd.DataFrame:
    """Rows where actual_trade_label = CALL/PUT but we predicted NO_POSITION."""
    predictions = _predictions_with_promotions(input_path, symbol)
    predictions = predictions[predictions["next_open"].notna()].copy()

    recall_misses = predictions[
        predictions["regime"].eq(regime)
        & predictions["effective_prediction"].eq("NO_POSITION")
        & predictions["actual_trade_label"].isin(["CALL", "PUT"])
    ].copy()

    if recall_misses.empty:
        print(f"No {regime} recall misses found.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        recall_misses.to_csv(output_path, index=False)
        return recall_misses

    # Merge signal-day features (including VIX via LEFT JOIN in _feature_rows)
    signal_dates = pd.to_datetime(recall_misses["signal_date"]).dt.date
    trade_dates = pd.to_datetime(recall_misses["next_trade_date"]).dt.date
    features = _feature_rows(sorted(set(signal_dates) | set(trade_dates)), symbol)

    # Flat merge on signal_date (no prefix — we use direct column names in diagnosis)
    features_flat = features.copy()
    if "feature_id" in features_flat.columns:
        features_flat = features_flat.drop(columns=["feature_id"])
    recall_misses["_signal_date_key"] = signal_dates
    recall_misses = recall_misses.merge(
        features_flat.rename(columns={"signal_date": "_signal_date_key"}),
        on="_signal_date_key",
        how="left",
        suffixes=("", "_feat"),
    )

    # Excursion pcts (how far the underlying actually moved on the missed day)
    recall_misses["up_excursion_pct"] = (
        (recall_misses["next_high"] - recall_misses["next_open"]) / recall_misses["next_open"] * 100
    ).round(2)
    recall_misses["down_excursion_pct"] = (
        (recall_misses["next_open"] - recall_misses["next_low"]) / recall_misses["next_open"] * 100
    ).round(2)

    # Per-strategy diagnosis
    recall_misses["why_not_predicted"] = recall_misses.apply(
        lambda row: _why_not_predicted(row, row["actual_trade_label"], regime), axis=1
    )
    recall_misses["why_missed"] = recall_misses.apply(
        lambda row: _why_missed_recall(row, row["actual_trade_label"], regime), axis=1
    )

    recall_misses = recall_misses.drop(columns=["_signal_date_key"], errors="ignore")

    front = [
        "signal_date", "next_trade_date", "regime", "actual_trade_label",
        "final_prediction", "watch_signal", "promoted_prediction",
        "effective_prediction", "promotion_reason",
        "primary_strategy_family", "primary_strategy_type",
        "watch_family", "watch_variant", "watch_strategy_type",
        "confirming_family", "confirming_variant", "confirming_strategy_type",
        "family_confirmation_match", "promotion_block_reason",
        "strength_score", "confidence_level",
        "global_risk_off", "global_gate_reason",
        "up_excursion_pct", "down_excursion_pct",
        "why_not_predicted", "why_missed",
        # key signal-day features for quick inspection
        "rsi14", "rsi5", "ret_5d", "ret_10d", "ret_3d",
        "ma20_slope", "ma10d_slope", "ma5d_slope",
        "bb_width", "bb_lower", "bb_upper",
        "range_position_10d", "resistance_distance_10d",
        "volume_day", "volume_20d",
        "vix_close", "vix_chg_1d",
    ]
    existing_front = [c for c in front if c in recall_misses.columns]
    rest = [c for c in recall_misses.columns if c not in existing_front]
    recall_misses = recall_misses[existing_front + rest]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    recall_misses.to_csv(output_path, index=False)
    return recall_misses


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate precision and recall miss CSVs for in-sample analysis."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--precision-output", type=Path, default=DEFAULT_PRECISION_OUTPUT)
    parser.add_argument("--recall-output", type=Path, default=DEFAULT_RECALL_OUTPUT)
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--regime", default="stress", choices=["stress", "calm"])
    parser.add_argument("--skip-precision", action="store_true")
    parser.add_argument("--skip-recall", action="store_true")
    args = parser.parse_args()

    if not args.skip_precision:
        result = generate(args.input, args.precision_output, args.symbol, args.regime)
        print(f"Wrote {len(result)} {args.regime} precision misses ->{args.precision_output}")

    if not args.skip_recall:
        # Update default output path to reflect regime
        recall_out = args.recall_output
        if recall_out == DEFAULT_RECALL_OUTPUT and args.regime != "stress":
            recall_out = recall_out.parent / recall_out.name.replace("stress", args.regime)
        result = generate_recall_misses(args.input, recall_out, args.symbol, args.regime)
        print(f"Wrote {len(result)} {args.regime} recall misses ->{recall_out}")


if __name__ == "__main__":
    main()
