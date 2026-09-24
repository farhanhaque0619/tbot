"""Position sizing. Deterministic. No Kelly, no model-derived sizing (see bot/research/kelly.py, research-only)."""
from __future__ import annotations

import math


def round_qty(qty: float, *, fractional: bool, decimals: int = 3) -> float:
    """Round DOWN to whole shares, or to ``decimals`` places when fractional shares are allowed."""
    if not math.isfinite(qty) or qty <= 0:
        return 0.0
    if not fractional:
        return float(math.floor(qty))
    factor = 10 ** decimals
    return math.floor(qty * factor + 1e-9) / factor


def fixed_fractional_qty(equity: float, price: float, stop_distance: float, *, risk_pct: float, max_position_pct: float,
                         cash_available: float | None = None, fractional: bool = False, decimals: int = 3,
                         max_notional: float | None = None) -> float:
    """Shares such that (price - stop) * qty ≈ risk_pct * equity, capped by notional limits.

    - ``stop_distance`` is the absolute distance from entry to protective stop.
    - The position notional never exceeds ``max_position_pct * equity`` (nor ``max_notional`` if given).
    - If ``cash_available`` is given, notional is also capped by it (no leverage).
    Returns whole shares unless ``fractional`` (then rounded down to ``decimals``). 0 if nothing sensible fits.
    """
    if equity <= 0 or price <= 0:
        return 0.0
    risk_dollars = risk_pct * equity
    qty = risk_dollars / stop_distance if stop_distance and stop_distance > 0 else math.inf
    cap = max_position_pct * equity
    if cash_available is not None:
        cap = min(cap, max(cash_available, 0.0))
    if max_notional is not None:
        cap = min(cap, max_notional)
    qty = min(qty, cap / price)
    return round_qty(qty, fractional=fractional, decimals=decimals)
