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
    ratchet_price: float | None = None,
    last_target_price: float | None = None,
) -> CascadeLevels:
    """Compute next target and stop-loss levels after a ratchet.

    Args:
        base_price: Previous target_price (cascade base). Used as fallback
            when ratchet_price is not supplied.
        completed_targets: Number of targets already hit (0-based before this call;
            caller passes the incremented value).
        target_pct: Base option-premium target percentage.
        sl_pct: Base stop-loss percentage.
        sl_divider: Kept for backward compatibility (unused in new formula).
        n_cap: Maximum effective_n for both target and SL scaling.
        ratchet_price: Live market bid at the moment of the ratchet. When
            supplied, both target and SL are anchored to this price rather than
            base_price. Defaults to base_price (pre-existing backtest behaviour).
        last_target_price: The target_price that was just hit. New SL is floored
            at this level (change 2: SL always >= last_target_price).
    """
    if sl_divider <= 0:
        raise ValueError("sl_divider must be greater than zero")

    effective_n = min(max(int(completed_targets), 0), max(int(n_cap), 0))
    price_base = float(ratchet_price) if ratchet_price is not None else float(base_price)

    # Decaying target multiplier: 1.0, 0.8, 0.6, … floor at 0.2 (change 3)
    target_multiplier = max(0.2, 1.0 - 0.2 * effective_n)
    target_price = price_base * (1.0 + target_multiplier * float(target_pct))

    # Increasing SL pct: sl_pct, 1.2*sl_pct, 1.4*sl_pct, … (change 4)
    adjusted_sl_pct = float(sl_pct) * (1.0 + 0.2 * effective_n)
    computed_sl = price_base * (1.0 - adjusted_sl_pct)

    # Floor: new SL must be >= previous target_price (change 2)
    if last_target_price is not None:
        stop_loss_price = max(computed_sl, float(last_target_price))
    else:
        stop_loss_price = computed_sl

    return CascadeLevels(
        base_price=float(base_price),
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        completed_targets=int(completed_targets),
        effective_n=effective_n,
        adjusted_sl_pct=adjusted_sl_pct,
    )
