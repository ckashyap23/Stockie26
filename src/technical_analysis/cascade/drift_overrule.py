"""
Drift-overrule module — thin post-cascade layer applied at 9:22 AM IST.

Reads the effective_prediction from the cascade and the 9:15-9:20 AM open-gap
features (nifty_drift_pct, nifty_gap_pct, gap_open_atr) and applies a quick
pre-market adjustment.  The original cascade prediction is never modified;
instead a new drift_effective_prediction is produced for downstream use.

Decision table
--------------
D = +1 (CALL) or -1 (PUT) matching effective_prediction / watch direction.

TRADE (CALL/PUT):
  drift confirms + gap confirms (|gap| > GAP_CONFIRM_MIN) -> TRADE at HALF_SIZE
  drift confirms (alone)                                  -> TRADE at FULL_SIZE
  drift opposes                                           -> NO_POSITION
  no drift (|drift| < DRIFT_MIN)                         -> NO_CHANGE

WATCH (CALL/PUT) [watch_signal set, promoted_prediction = NO_POSITION]:
  drift confirms                                          -> TRADE at HALF_SIZE
  otherwise                                              -> NO_CHANGE

NO_POSITION:
  Path 1 – drift-led probe:
    |drift| >= DRIFT_PROBE_MIN AND sign(drift)==sign(gap)
    AND not event_day AND not family_suspended           -> TRADE(drift dir) HALF_SIZE
  Path 2 – tail-shock:
    |gap_open_atr| > TAIL_SHOCK_ATR AND vix_gap > 0
    AND global_confirm AND not event_day                 -> TRADE(gap dir) HALF_SIZE

All thresholds are module-level constants; override via env-prefixed vars if needed.

Assumptions on undefined inputs:
  vix_gap       = vix_chg_1d > 0  (VIX was rising into D-1 close)
  global_confirm = sign(global_asia_overnight_return_mean) == sign(gap)
    (confirmed: Asia overnight gap aligned with NIFTY gap)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.common.config import get_drift_probe_min_pct, get_drift_probe_half_min_pct, get_drift_probe_half_min_pct

# ── Thresholds (tune via constants; add env-var overrides as needed) ──────────
DRIFT_MIN        = 0.001   # 0.10%: minimum |drift| to count as directional
GAP_CONFIRM_MIN  = 0.003   # 0.30%: |gap| threshold for "confirmed" sub-case in TRADE
# DRIFT_PROBE_MIN is read from env at runtime via get_drift_probe_min_pct() (DRIFT_PROBE_MIN_PCT)
TAIL_SHOCK_ATR   = 1.5     # gap_open_atr threshold for tail-shock path
HALF_SIZE        = 0.5     # position size multiplier when overruled

CALL = "CALL"
PUT  = "PUT"
NO_POSITION = "NO_POSITION"


@dataclass
class DriftInputs:
    """All inputs required for the drift-overrule decision."""
    effective_prediction:  str          # cascade output: CALL / PUT / NO_POSITION
    watch_signal:          str | None   # CALL_3D_WATCH / PUT_3D_WATCH / None
    promoted_prediction:   str          # CALL / PUT / NO_POSITION
    nifty_drift_pct:       float | None
    nifty_gap_pct:         float | None
    gap_open_atr:          float | None
    vix_chg_1d:            float | None  # vix_gap proxy: positive = VIX rising
    global_asia_overnight_return_mean: float | None  # for global_confirm in tail-shock
    event_gate_reason:     str | None    # non-null → event-impact day gate
    is_family_suspended:   bool          # cooloff active (blocks NO_POSITION paths)
    base_position_size_pct: float        # PAPER_CAPITAL_PER_TRADE_PCT from settings


@dataclass
class DriftResult:
    drift_effective_prediction: str
    drift_position_size_pct:    float
    drift_overrule_reason:      str


def _sign(x: float) -> int:
    if x > 0: return 1
    if x < 0: return -1
    return 0


def _direction_of(prediction: str) -> int:
    """+1 for CALL, -1 for PUT, 0 for anything else."""
    if prediction == CALL: return 1
    if prediction == PUT:  return -1
    return 0


def _watch_direction(watch_signal: str | None) -> int:
    """Infer watch direction from watch_signal string."""
    if watch_signal and "CALL" in watch_signal: return 1
    if watch_signal and "PUT"  in watch_signal: return -1
    return 0


def apply_drift_overrule(inputs: DriftInputs) -> DriftResult:
    """Apply drift-overrule logic and return the adjusted direction + sizing."""
    drift  = inputs.nifty_drift_pct  or 0.0
    gap    = inputs.nifty_gap_pct    or 0.0
    g_atr  = inputs.gap_open_atr     or 0.0
    vix_g  = inputs.vix_chg_1d       or 0.0
    g_ret  = inputs.global_asia_overnight_return_mean or 0.0
    event  = bool(inputs.event_gate_reason)
    susp   = inputs.is_family_suspended
    base   = inputs.base_position_size_pct

    drift_dir = _sign(drift) if abs(drift) >= DRIFT_MIN else 0

    ep = inputs.effective_prediction
    ws = inputs.watch_signal
    pp = inputs.promoted_prediction

    # Detect WATCH state: watch active but not yet promoted to a trade
    is_watch = (
        ws is not None
        and ws not in ("", "None")
        and ep == NO_POSITION
        and pp == NO_POSITION
    )
    if is_watch:
        watch_dir = _watch_direction(ws)
    else:
        watch_dir = 0

    # ── TRADE ──────────────────────────────────────────────────────────────────
    if ep in (CALL, PUT):
        D = _direction_of(ep)
        gap_confirmed = _sign(gap) == D and abs(gap) >= GAP_CONFIRM_MIN

        if drift_dir == D and gap_confirmed:
            return DriftResult(ep, HALF_SIZE, "DRIFT_CONFIRMS_HALF_SIZE")
        if drift_dir == D:
            return DriftResult(ep, base, "DRIFT_CONFIRMS_FULL")
        # drift opposes OR no drift — keep cascade direction unchanged
        return DriftResult(ep, base, "DRIFT_NONE_NO_CHANGE")

    # ── WATCH ──────────────────────────────────────────────────────────────────
    if is_watch:
        if drift_dir == watch_dir and watch_dir != 0:
            direction = CALL if watch_dir > 0 else PUT
            return DriftResult(direction, HALF_SIZE, "DRIFT_PROMOTES_WATCH")
        return DriftResult(NO_POSITION, 0.0, "WATCH_NO_DRIFT_CONFIRM")

    # ── NO_POSITION ─────────────────────────────────────────────────────────────
    # Path 1: drift-led probe — fires on drift alone (no gap alignment required).
    # Size = full base if gap also aligns (confirming); half-size if gap is absent
    # or contradicts (drift-only signal, less conviction).
    probe_min = get_drift_probe_min_pct()
    if (
        abs(drift) >= probe_min
        and not event
        and not susp
    ):
        direction = CALL if drift > 0 else PUT
        gap_aligned = _sign(drift) == _sign(gap) and _sign(gap) != 0
        size = base if gap_aligned else HALF_SIZE
        # Half-size quality gate: when gap doesn’t confirm, require a stronger drift
        # signal to avoid low-conviction noise probes (default: 0.20%).
        if size == HALF_SIZE and abs(drift) < get_drift_probe_half_min_pct():
            return DriftResult(NO_POSITION, 0.0, "NO_CHANGE")
        return DriftResult(direction, size, "DRIFT_PROBE")

    # Path 2: tail-shock — MUTED (25% precision over backtest, net negative)
    # global_confirm = (_sign(g_ret) == _sign(gap)) and _sign(gap) != 0
    # if (abs(g_atr) > TAIL_SHOCK_ATR and vix_g > 0 and global_confirm and not event):
    #     direction = CALL if gap > 0 else PUT
    #     return DriftResult(direction, HALF_SIZE, "TAIL_SHOCK")

    return DriftResult(NO_POSITION, 0.0, "NO_CHANGE")


def load_drift_inputs(
    db_row: dict[str, Any],
    gap_features: dict[str, Any],
    base_position_size_pct: float,
) -> DriftInputs:
    """Build DriftInputs from a NiftyPrediction row + SignalFeatureDaily gap cols.

    db_row        : NiftyPrediction dict (must include watch_signal, promoted_prediction,
                    vix_chg_1d, global_asia_overnight_return_mean, event_gate_reason)
    gap_features  : dict with nifty_drift_pct, nifty_gap_pct, gap_open_atr
                    (loaded from SignalFeatureDaily for signal_date = D-1)
    """
    return DriftInputs(
        effective_prediction  = str(db_row.get("effective_prediction") or NO_POSITION),
        watch_signal          = db_row.get("watch_signal"),
        promoted_prediction   = str(db_row.get("promoted_prediction") or NO_POSITION),
        nifty_drift_pct       = _safe_float(gap_features.get("nifty_drift_pct")),
        nifty_gap_pct         = _safe_float(gap_features.get("nifty_gap_pct")),
        gap_open_atr          = _safe_float(gap_features.get("gap_open_atr")),
        vix_chg_1d            = _safe_float(db_row.get("vix_chg_1d")),
        global_asia_overnight_return_mean = _safe_float(db_row.get("global_asia_overnight_return_mean")),
        event_gate_reason     = db_row.get("event_gate_reason") or None,
        is_family_suspended   = bool(db_row.get("promotion_block_reason", "")
                                     and "COOLOFF" in str(db_row.get("promotion_block_reason", ""))),
        base_position_size_pct = base_position_size_pct,
    )


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None
