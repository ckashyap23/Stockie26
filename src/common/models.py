from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

@dataclass
class StockInstrument:
    exchange: str
    tradingsymbol: str
    name: str | None
    instrument_token: int
    segment: str | None
    tick_size: float | None
    lot_size: int | None

@dataclass
class OptionInstrument:
    fetch_date: date
    underlying: str
    exchange: str
    tradingsymbol: str
    instrument_token: int
    name: str | None
    strike: float
    expiry: date
    instrument_type: str
    lot_size: int
    tick_size: float | None
    segment: str | None


@dataclass
class OptionSnapshot:
    """
    One raw snapshot from Kite for a given option instrument.

    Maps 1:1 to dbo.OptionSnapshot in SQL:
      - id is DB identity PK (can be None before insert)
    """
    id: int | None            # DB PK; set after insert
    option_instrument_id: int # FK -> OptionInstrument (your DB id)
    snapshot_time: datetime

    # raw data from Kite
    underlying_price: float | None
    last_price: float | None
    bid_price: float | None
    bid_qty: int | None
    ask_price: float | None
    ask_qty: int | None
    volume: int | None
    open_interest: int | None


# -------------------------
# NEW: calculated table
# -------------------------

@dataclass
class OptionSnapshotCalc:
    """
    Calculated analytics (IV + Greeks) for a given snapshot.

    Maps 1:1 to dbo.OptionSnapshotCalc:
      - option_snapshot_id FK -> OptionSnapshot.id
    """
    option_snapshot_id: int   # FK -> OptionSnapshot.id
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None


# -------------------------------------------------
# OPTIONAL: read model combining both via a JOIN
# -------------------------------------------------

@dataclass
class OptionData:
    """
    Convenience view used when READING:
    result of joining OptionSnapshot + OptionSnapshotCalc.
    (Not a separate table.)
    """
    option_instrument_id: int
    snapshot_time: datetime

    underlying_price: float | None
    last_price: float | None
    bid_price: float | None
    bid_qty: int | None
    ask_price: float | None
    ask_qty: int | None
    volume: int | None
    open_interest: int | None

    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None


# ============================================================
# NEW: Trading Signal Engine + Option Selector tables
# ============================================================

@dataclass
class WatchedInstrument:
    tradingsymbol: str
    exchange: str
    instrument_type: str          # STOCK, INDEX, SECTOR_INDEX
    name: str | None = None
    instrument_token: int | None = None
    segment: str | None = None
    tick_size: float | None = None
    lot_size: int | None = None
    sector: str | None = None
    industry: str | None = None
    is_fo_enabled: bool = False
    is_active: bool = True
    watched_id: int | None = None  # DB PK; set after insert


@dataclass
class TradingCalendarEntry:
    calendar_date: date
    exchange: str
    is_trading_day: bool
    is_weekly_expiry: bool = False
    is_monthly_expiry: bool = False
    is_special_session: bool = False
    notes: str | None = None


@dataclass
class SignalFeatureDaily:
    signal_date: date
    symbol: str
    feature_version: str = "v1"
    instrument_type: str | None = None
    open_915: float | None = None
    close_1515: float | None = None
    high_day: float | None = None
    low_day: float | None = None
    volume_day: int | None = None
    ret_1d: float | None = None
    ret_3d: float | None = None
    ret_5d: float | None = None
    ret_10d: float | None = None
    ret_20d: float | None = None
    ret_60d: float | None = None
    ma5d_slope: float | None = None
    ma10d_slope: float | None = None
    ma20_slope: float | None = None
    ma50_slope: float | None = None
    ma_slope_combo: float | None = None
    sma_5: float | None = None
    sma_10: float | None = None
    sma_20: float | None = None
    ema_5: float | None = None
    ema_10: float | None = None
    ema_20: float | None = None
    close_vs_sma_5: float | None = None
    close_vs_sma_10: float | None = None
    close_vs_sma_20: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    roc_5: float | None = None
    roc_10: float | None = None
    atr_14: float | None = None
    atr_pct: float | None = None
    realized_vol_5d: float | None = None
    realized_vol_10d: float | None = None
    realized_vol_20d: float | None = None
    volatility_10d: float | None = None
    volatility_20d: float | None = None
    volume_10d: float | None = None
    volume_20d: float | None = None
    volume_hybrid: float | None = None
    trend_efficiency_5d: float | None = None
    trend_efficiency_10d: float | None = None
    trend_efficiency_20d: float | None = None
    trend_efficiency_60d: float | None = None
    recent_high_5d: float | None = None
    recent_low_5d: float | None = None
    recent_high_10d: float | None = None
    resistance_distance_10d: float | None = None
    recent_low_10d: float | None = None
    recent_high_20d: float | None = None
    recent_low_20d: float | None = None
    range_position_5d: float | None = None
    range_position_10d: float | None = None
    range_position_20d: float | None = None
    day_range_pct: float | None = None
    gap_pct: float | None = None
    close_position_in_range: float | None = None
    futures_oi_change_pct: float | None = None
    futures_volume_change_pct: float | None = None
    pcr_oi: float | None = None
    pcr_volume: float | None = None
    atm_iv: float | None = None
    iv_rank_20d: float | None = None
    skew_put_call: float | None = None
    max_oi_call_strike: float | None = None
    max_oi_put_strike: float | None = None
    distance_from_max_call_oi_pct: float | None = None
    distance_from_max_put_oi_pct: float | None = None
    macro_score: float | None = None
    news_score: float | None = None
    event_risk_score: float | None = None
    regime: str | None = None
    source_quality_score: float | None = None
    reason_json: str | None = None
    strategy_features_json: str | None = None
    feature_id: int | None = None  # DB PK; set after insert


@dataclass
class SignalPrediction:
    signal_date: date
    trade_date: date
    symbol: str
    model_name: str
    model_version: str
    direction: str                # UP, DOWN, NO_TRADE
    confidence: float
    instrument_type: str | None = None
    expected_move_pct: float | None = None
    trade_allowed: bool = False
    no_trade_reason: str | None = None
    regime: str | None = None
    feature_id: int | None = None
    reason_json: str | None = None
    prediction_id: int | None = None  # DB PK; set after insert


@dataclass
class SignalBacktestLabel:
    signal_date: date
    trade_date: date
    symbol: str
    label_version: str = "v1"
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    realized_return_pct: float | None = None
    positive_threshold_pct: float | None = None
    negative_threshold_pct: float | None = None
    actual_label: str | None = None   # UP, DOWN, NO_TRADE
    label_id: int | None = None       # DB PK; set after insert


@dataclass
class OptionCandle5m:
    option_instrument_id: int
    tradingsymbol: str
    underlying: str
    candle_time: datetime
    data_purpose: str             # BACKTEST_CANDIDATE, PAPER_TRADE, LIVE_TRADE, MANUAL_BACKFILL
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    close_price: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    source: str | None = None
    option_candle_id: int | None = None  # DB PK; set after insert
