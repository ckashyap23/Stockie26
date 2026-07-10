"""Execution-time gates that may use D1 opening and live intraday prices."""
from __future__ import annotations

from dataclasses import dataclass


CALL_NEGATIVE_GAP_PCT = -0.002
CALL_RECLAIM_BUFFER_PCT = 0.001


@dataclass(frozen=True)
class EntryGateDecision:
    entry_action: str
    allow_entry: bool
    opening_gap_pct: float | None = None
    reclaim_level: float | None = None
    reason: str = ""


def evaluate_promoted_call_entry(
    *,
    final_prediction: str | None,
    promoted_prediction: str | None,
    signal_day_close_1515: float | None,
    d1_open: float | None,
    current_spot: float | None,
) -> EntryGateDecision:
    """Require an intraday reclaim after an adverse opening gap on promoted CALLs."""
    is_promoted_call = final_prediction == "NO_POSITION" and promoted_prediction == "CALL"
    if not is_promoted_call:
        return EntryGateDecision("ENTER", True)
    if not signal_day_close_1515 or not d1_open:
        return EntryGateDecision(
            "WAIT_FOR_CALL_RECLAIM_DATA", False, reason="Missing signal close or D1 open"
        )

    gap_pct = d1_open / signal_day_close_1515 - 1.0
    reclaim_level = round(signal_day_close_1515 * (1.0 + CALL_RECLAIM_BUFFER_PCT), 4)
    if gap_pct > CALL_NEGATIVE_GAP_PCT:
        return EntryGateDecision("ENTER", True, gap_pct, reclaim_level)
    if current_spot is not None and current_spot >= reclaim_level:
        return EntryGateDecision(
            "ENTER_CALL_RECLAIMED", True, gap_pct, reclaim_level,
            "Adverse gap reclaimed above signal close +0.10%",
        )
    return EntryGateDecision(
        "WAIT_FOR_CALL_RECLAIM", False, gap_pct, reclaim_level,
        "Promoted CALL opened <= -0.20%; waiting for intraday reclaim",
    )
