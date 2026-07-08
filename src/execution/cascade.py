from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CascadeLevels:
    base_price: float
    target_price: float
    stop_loss_price: float
    completed_targets: int
    effective_n: int
    adjusted_sl_pct: float


def compute_cascade_levels(
    base_price: float,
    completed_targets: int,
    target_pct: float,
    sl_pct: float,
    sl_divider: float,
    n_cap: int,
) -> CascadeLevels:
    if sl_divider <= 0:
        raise ValueError("sl_divider must be greater than zero")
    effective_n = min(max(int(completed_targets), 0), max(int(n_cap), 0))
    adjusted_sl_pct = float(sl_pct) * (1 + effective_n / float(sl_divider))
    return CascadeLevels(
        base_price=float(base_price),
        target_price=float(base_price) * (1 + float(target_pct)),
        stop_loss_price=float(base_price) * (1 - adjusted_sl_pct),
        completed_targets=int(completed_targets),
        effective_n=effective_n,
        adjusted_sl_pct=adjusted_sl_pct,
    )
