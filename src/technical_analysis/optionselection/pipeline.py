from __future__ import annotations

from datetime import date
from typing import Any

from src.technical_analysis.optionselection.option_selector import select_option_strategy
from src.technical_analysis.optionselection.schema import OptionSelectionResult
from src.technical_analysis.prediction.schema import UnderlyingView

def target_pcts_for_regime(regime: str | None) -> tuple[float, float]:
    """Return (target_1_pct, target_2_pct) for the regime, read from env via config."""
    from src.common.config import get_target_pcts_for_regime
    return get_target_pcts_for_regime(regime)


def run_option_selection_from_db(
    db_client,
    underlying: str = "NIFTY",
    trade_date: str | None = None,
    model_version: str = "cascade_v1",
    target_pcts: tuple[float, float] | None = None,
    stop_loss_pct: float | None = None,
) -> dict[str, Any]:
    prediction = fetch_prediction_row(db_client.conn, underlying, model_version, trade_date)
    if prediction is None:
        raise RuntimeError(
            f"No NiftyPrediction row found for {underlying} "
            f"model_version={model_version} trade_date={trade_date or '<latest>'}"
        )

    view = prediction_to_underlying_view(prediction, underlying)
    spot_price = _float_or_none(prediction.get("close_1515")) or 0.0
    as_of_time = f"{prediction['trade_date']} 15:15:00"
    iv_history = fetch_atm_iv_history(db_client.conn, underlying, spot_price, _to_date(prediction["trade_date"])) if spot_price > 0 else []
    result = select_option_strategy(db_client, view, spot_price, as_of_time, iv_history or None)
    row = option_selection_to_row(
        prediction,
        result,
        underlying,
        model_version,
        spot_price,
        as_of_time,
        target_pcts=target_pcts,
        stop_loss_pct=stop_loss_pct,
    )
    written = db_client.upsert_nifty_option_selections([row])
    return {"rows": written, "selection": row}


def fetch_prediction_row(conn, underlying: str, model_version: str, trade_date: str | None) -> dict[str, Any] | None:
    """Fetch the NiftyPrediction row for option selection.

    Lookup order when trade_date is supplied:
      1. By signal trade_date (exact match) — the standard case when running on the signal day.
      2. By next_trade_date (execution date) — handles the case where the user passes the
         execution/paper-trade date (e.g. 2026-07-02) instead of the signal date (2026-07-01).
    When trade_date is None, returns the latest row.
    """
    base_cols = """
        symbol, trade_date, model_version, next_trade_date,
        close_1515, regime, final_prediction, direction, volatility_regime,
        primary_strategy, strategy_precision, signal_style,
        strength_score, strength_label, confidence_level
    """

    def _run(sql: str, params: tuple) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
        if row is None:
            return None
        return dict(zip(cols, row, strict=False))

    if not trade_date:
        sql = f"""
            SELECT {base_cols}
            FROM "NiftyPrediction"
            WHERE UPPER(symbol) = %s AND model_version = %s
            ORDER BY trade_date DESC LIMIT 1
        """
        return _run(sql, (underlying.upper(), model_version))

    # Try signal trade_date first
    sql = f"""
        SELECT {base_cols}
        FROM "NiftyPrediction"
        WHERE UPPER(symbol) = %s AND model_version = %s AND trade_date = %s
        ORDER BY trade_date DESC LIMIT 1
    """
    result = _run(sql, (underlying.upper(), model_version, trade_date))
    if result is not None:
        return result

    # Fall back: treat trade_date as execution/next_trade_date
    sql = f"""
        SELECT {base_cols}
        FROM "NiftyPrediction"
        WHERE UPPER(symbol) = %s AND model_version = %s AND next_trade_date = %s
        ORDER BY trade_date DESC LIMIT 1
    """
    return _run(sql, (underlying.upper(), model_version, trade_date))


def prediction_to_underlying_view(row: dict[str, Any], underlying: str) -> UnderlyingView:
    prediction_side = str(row.get("direction") or row.get("final_prediction") or "NO_POSITION")
    if prediction_side not in {"CALL", "PUT"}:
        prediction_side = "NO_POSITION"
    internal_direction = "BULLISH" if prediction_side == "CALL" else "BEARISH" if prediction_side == "PUT" else "NEUTRAL"
    strength_score = _float_or_none(row.get("strength_score")) or 0.0
    return UnderlyingView(
        symbol=underlying.upper(),
        trade_date=str(row["trade_date"]),
        raw_signal=prediction_side,  # type: ignore[arg-type]
        direction=internal_direction,  # type: ignore[arg-type]
        stock_regime="UNKNOWN",
        sector_regime=None,
        benchmark_regime=None,
        primary_strategy=str(row.get("primary_strategy") or "") or None,
        setup_type="NO_SETUP",
        strength_score=strength_score,
        confidence=_confidence_from_level(row.get("confidence_level")),  # type: ignore[arg-type]
        expected_move_pct=None,
        expected_move_abs=None,
        expected_holding_days=3 if prediction_side in {"CALL", "PUT"} else 0,
        atr14=None,
        volatility_20d=None,
        volume_ratio=None,
        relative_strength_vs_sector=None,
        relative_strength_vs_benchmark=None,
        stock_technical_score=0,
        sector_confirmation_score=0,
        benchmark_confirmation_score=0,
        relative_strength_score=0,
        volume_confirmation_score=0,
        risk_quality_score=0,
        regime_quality_score=0,
        strategy_signals=[],
        reasons=[],
        warnings=[],
        is_option_eligible=False,
        option_bias="NEUTRAL",
    )


def option_selection_to_row(
    prediction: dict[str, Any],
    result: OptionSelectionResult,
    underlying: str,
    model_version: str,
    spot_price: float,
    as_of_time: str,
    target_pcts: tuple[float, float] | None = None,
    stop_loss_pct: float | None = None,
) -> dict[str, Any]:
    candidate = result.selected_strategy
    first_buy = next((leg for leg in candidate.legs if leg.side == "BUY"), None)
    buy_price = first_buy.contract.last_price if first_buy else None
    resolved_target_pcts = target_pcts or target_pcts_for_regime(
        prediction.get("volatility_regime") or prediction.get("regime")
    )
    target_1_pct = resolved_target_pcts[0] if len(resolved_target_pcts) > 0 else None
    target_2_pct = resolved_target_pcts[1] if len(resolved_target_pcts) > 1 else None
    stop_loss_enabled = stop_loss_pct is not None and stop_loss_pct > 0
    legs_summary = "; ".join(
        f"{leg.side} {leg.contract.tradingsymbol} @{leg.contract.last_price}"
        for leg in candidate.legs
    ) if candidate.legs else ""
    return {
        "symbol": underlying.upper(),
        "trade_date": str(prediction["trade_date"]),
        "model_version": model_version,
        "next_trade_date": _date_str_or_none(prediction.get("next_trade_date")),
        "final_prediction": prediction.get("final_prediction"),
        "prediction_direction": prediction.get("direction") or prediction.get("final_prediction"),
        "volatility_regime": prediction.get("volatility_regime") or prediction.get("regime"),
        "primary_strategy": prediction.get("primary_strategy"),
        "strategy_precision": _float_or_none(prediction.get("strategy_precision")),
        "signal_style": prediction.get("signal_style"),
        "strength_score": _float_or_none(prediction.get("strength_score")),
        "strength_label": prediction.get("strength_label"),
        "confidence_level": _float_or_none(prediction.get("confidence_level")),
        "spot_price": spot_price,
        "as_of_time": as_of_time,
        "selected_strategy": candidate.strategy_type,
        "option_bias_selected": result.option_bias,
        "no_trade_reason": result.no_trade_reason,
        "evaluated_candidate_count": result.evaluated_candidate_count,
        "strategy_direction": candidate.direction,
        "entry_debit_or_credit": candidate.entry_debit_or_credit,
        "max_profit": candidate.max_profit,
        "max_loss": candidate.max_loss,
        "breakeven": candidate.breakeven,
        "reward_risk": candidate.reward_risk,
        "selection_score": candidate.score,
        "selection_confidence": candidate.confidence,
        "total_delta": candidate.total_delta,
        "total_gamma": candidate.total_gamma,
        "total_theta": candidate.total_theta,
        "total_vega": candidate.total_vega,
        "legs_summary": legs_summary,
        "primary_buy_token": first_buy.contract.instrument_token if first_buy else None,
        "primary_buy_symbol": first_buy.contract.tradingsymbol if first_buy else None,
        "primary_buy_strike": first_buy.contract.strike if first_buy else None,
        "primary_buy_expiry": first_buy.contract.expiry if first_buy else None,
        "primary_buy_option_type": first_buy.contract.option_type if first_buy else None,
        "primary_buy_entry_price": buy_price,
        "primary_buy_iv": first_buy.contract.iv if first_buy else None,
        "primary_buy_delta": first_buy.contract.delta if first_buy else None,
        "target_1_pct": target_1_pct,
        "target_1_price": _price_with_pct(buy_price, target_1_pct),
        "target_2_pct": target_2_pct,
        "target_2_price": _price_with_pct(buy_price, target_2_pct),
        "stop_loss_enabled": stop_loss_enabled,
        "stop_loss_pct": stop_loss_pct if stop_loss_enabled else None,
        "stop_loss_price": _price_with_pct(buy_price, -stop_loss_pct) if stop_loss_enabled else None,
    }


def fetch_atm_iv_history(conn, underlying: str, spot_price: float, as_of_date: date) -> list[float]:
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


def _confidence_from_level(value: Any) -> str:
    level = _float_or_none(value)
    if level is None:
        return "LOW"
    if level >= 0.80:
        return "HIGH"
    if level >= 0.65:
        return "MEDIUM"
    return "LOW"


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_with_pct(price: float | None, pct: float | None) -> float | None:
    if price is None or pct is None:
        return None
    return round(float(price) * (1.0 + float(pct)), 2)


def _to_date(value: Any) -> date:
    return value if isinstance(value, date) else value.date() if hasattr(value, "date") else date.fromisoformat(str(value))


def _date_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
