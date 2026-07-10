from __future__ import annotations

import math


def size_long_option_position(
    entry_price: float,
    lot_size: int,
    trading_capital: float,
    capital_per_trade_pct: float,
) -> tuple[int, int]:
    """Return (lot_count, quantity) without exceeding allocated premium capital."""
    if entry_price <= 0 or lot_size <= 0 or trading_capital <= 0:
        raise ValueError("entry_price, lot_size, and trading_capital must be positive")
    if not 0 < capital_per_trade_pct <= 1:
        raise ValueError("capital_per_trade_pct must be between 0 and 1")
    allocated = trading_capital * capital_per_trade_pct
    lot_count = math.floor(allocated / (entry_price * lot_size))
    if lot_count < 1:
        raise ValueError(
            f"Allocated capital {allocated:.2f} cannot fund one lot costing "
            f"{entry_price * lot_size:.2f}"
        )
    return lot_count, lot_count * lot_size
