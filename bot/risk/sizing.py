"""Position sizing."""
from __future__ import annotations

import math


def fixed_fractional_qty(equity: float, price: float, stop_distance: float, *,
                         risk_pct: float, max_position_pct: float, cash_available: float | None = None) -> int:
    """Shares such that (price - stop) * qty ≈ risk_pct * equity, capped by notional limits.

    - ``stop_distance`` is the absolute distance from entry to protective stop.
    - The position notional never exceeds ``max_position_pct * equity``.
    - If ``cash_available`` is given, notional is also capped by it (no leverage).
    Returns whole shares (0 if nothing sensible fits).
    """
    if equity <= 0 or price <= 0:
        return 0
    risk_dollars = risk_pct * equity
    if stop_distance and stop_distance > 0:
        qty = risk_dollars / stop_distance
    else:  # no stop info -> fall back to the notional cap only
        qty = math.inf
    max_notional = max_position_pct * equity
    if cash_available is not None:
        max_notional = min(max_notional, max(cash_available, 0.0))
    qty = min(qty, max_notional / price)
    return int(math.floor(qty)) if math.isfinite(qty) else 0
