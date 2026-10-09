from __future__ import annotations

from dataclasses import dataclass


DEFAULT_OPTION_DELTA = 0.58


@dataclass(frozen=True)
class DeltaAdjustedLevels:
    entry_price: float
    underlying_price: float
    option_delta: float
    target_price: float
    stop_loss_price: float
    target_move: float
    stop_loss_move: float


@dataclass(frozen=True)
class AnchoredCascadeLevels:
    base_price: float
    target_price: float
    stop_loss_price: float
    completed_targets: int
    effective_n: int
    adjusted_stop_move: float


def compute_delta_adjusted_levels(
    entry_price: float,
    underlying_price: float,
    option_delta: float | None,
    target_pct: float,
    stop_loss_pct: float,
) -> DeltaAdjustedLevels:
    entry = float(entry_price)
    underlying = float(underlying_price)
    if entry <= 0:
        raise ValueError("entry_price must be greater than zero")
    if underlying <= 0:
        raise ValueError("underlying_price must be greater than zero")
    if float(target_pct) <= 0:
        raise ValueError("target_pct must be greater than zero")
    if float(stop_loss_pct) <= 0:
        raise ValueError("stop_loss_pct must be greater than zero")

    delta = abs(float(option_delta)) if option_delta is not None else DEFAULT_OPTION_DELTA
    if not 0 < delta <= 1:
        delta = DEFAULT_OPTION_DELTA

    target_move = delta * underlying * float(target_pct)
    stop_loss_move = delta * underlying * float(stop_loss_pct)
    return DeltaAdjustedLevels(
        entry_price=entry,
        underlying_price=underlying,
        option_delta=delta,
        target_price=entry + target_move,
        stop_loss_price=max(0.0, entry - stop_loss_move),
        target_move=target_move,
        stop_loss_move=stop_loss_move,
    )


def compute_anchored_cascade_levels(
    base_price: float,
    initial_target_move: float,
    initial_stop_move: float,
    completed_targets: int,
    sl_divider: float,
    n_cap: int,
) -> AnchoredCascadeLevels:
    if float(base_price) <= 0:
        raise ValueError("base_price must be greater than zero")
    if float(initial_target_move) <= 0:
        raise ValueError("initial_target_move must be greater than zero")
    if float(initial_stop_move) <= 0:
        raise ValueError("initial_stop_move must be greater than zero")
    if float(sl_divider) <= 0:
        raise ValueError("sl_divider must be greater than zero")

    effective_n = min(max(int(completed_targets), 0), max(int(n_cap), 0))
    adjusted_stop_move = float(initial_stop_move) * (
        1 + effective_n / float(sl_divider)
    )
    base = float(base_price)
    return AnchoredCascadeLevels(
        base_price=base,
        target_price=base + float(initial_target_move),
        stop_loss_price=max(0.0, base - adjusted_stop_move),
        completed_targets=int(completed_targets),
        effective_n=effective_n,
        adjusted_stop_move=adjusted_stop_move,
    )


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
