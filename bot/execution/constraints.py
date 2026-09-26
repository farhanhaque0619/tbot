"""Documented Alpaca order constraints, enforced in code BEFORE any API call (spec §8).

Sources: Alpaca docs "Orders" (fractional trading: DAY only, market/limit only, no advanced order classes; extended
hours: limit DAY/GTC only; OPG accepted before 09:28 or after 19:00 ET; CLS before 15:50 or after 19:00 ET; bracket/OTO
stop and take-profit at least $0.01 away from the base price; prices in $0.01 ticks at or above $1, $0.0001 below;
notional orders cannot be replaced). These are checks on what we send; the API remains the final authority and an
API rejection is still handled by the caller.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

OPG_CUTOFF, CLS_CUTOFF, AFTER_HOURS_OPEN = time(9, 28), time(15, 50), time(19, 0)
ORDER_TYPES = ("market", "limit", "stop", "stop_limit")
TIFS = ("day", "gtc", "opg", "cls", "ioc", "fok")
ORDER_CLASSES = ("simple", "oto", "bracket")


class OrderConstraintError(ValueError):
    """The order would be rejected by Alpaca for a documented reason; nothing was sent."""


def is_whole(qty: float) -> bool:
    return abs(qty - round(qty)) < 1e-9 and qty >= 1


def tick_for(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def round_to_tick(price: float) -> float:
    """Round half up to the tick (Decimal arithmetic: 0.12345 -> 0.1235, 100.005 -> 100.01)."""
    from decimal import ROUND_HALF_UP, Decimal
    tick = Decimal("0.01") if price >= 1.0 else Decimal("0.0001")
    return float(Decimal(repr(price)).quantize(tick, rounding=ROUND_HALF_UP))


def _wall(now: datetime | None) -> time | None:
    if now is None:
        return None
    return now.timetz().replace(tzinfo=None)


def validate_order(*, symbol: str, qty: float | None, side: str, order_type: str, tif: str, limit_price: float | None = None,
                   stop_price: float | None = None, take_profit_price: float | None = None, order_class: str = "simple",
                   extended_hours: bool = False, notional: float | None = None, base_price: float | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """Validate and normalise an order. Returns the fields to send (prices rounded to the tick). Raises OrderConstraintError."""
    if not symbol or not symbol.isupper() or not symbol.replace(".", "").isalnum():
        raise OrderConstraintError(f"bad symbol {symbol!r}")
    if side not in ("buy", "sell"):
        raise OrderConstraintError(f"side must be buy or sell, got {side!r}")
    if order_type not in ORDER_TYPES:
        raise OrderConstraintError(f"order_type {order_type!r} not in {ORDER_TYPES}")
    if tif not in TIFS:
        raise OrderConstraintError(f"time_in_force {tif!r} not in {TIFS}")
    if order_class not in ORDER_CLASSES:
        raise OrderConstraintError(f"order_class {order_class!r} not in {ORDER_CLASSES}")
    if (qty is None) == (notional is None):
        raise OrderConstraintError("exactly one of qty or notional is required")
    if qty is not None and qty <= 0:
        raise OrderConstraintError("qty must be positive")
    if notional is not None and notional <= 0:
        raise OrderConstraintError("notional must be positive")
    fractional = notional is not None or (qty is not None and not is_whole(qty))
    if fractional:
        if tif != "day":
            raise OrderConstraintError("fractional/notional orders must be DAY orders (no GTC, OPG, CLS, IOC, FOK)")
        if order_type not in ("market", "limit"):
            raise OrderConstraintError("fractional/notional orders must be market or limit orders")
        if order_class != "simple":
            raise OrderConstraintError("fractional/notional orders cannot be part of a bracket or OTO")
        if extended_hours:
            raise OrderConstraintError("fractional/notional orders are not accepted in extended hours")
    if extended_hours:
        if order_type != "limit" or tif not in ("day", "gtc"):
            raise OrderConstraintError("extended-hours orders must be LIMIT orders with DAY or GTC time in force")
    wall = _wall(now)
    if tif == "opg":
        if order_type not in ("market", "limit"):
            raise OrderConstraintError("OPG supports market and limit orders only")
        if wall is not None and not (wall < OPG_CUTOFF or wall >= AFTER_HOURS_OPEN):
            raise OrderConstraintError(f"OPG orders are accepted before 09:28 or after 19:00 ET (now {wall:%H:%M} ET)")
    if tif == "cls":
        if order_type not in ("market", "limit"):
            raise OrderConstraintError("CLS supports market and limit orders only")
        if wall is not None and not (wall < CLS_CUTOFF or wall >= AFTER_HOURS_OPEN):
            raise OrderConstraintError(f"CLS orders are accepted before 15:50 or after 19:00 ET (now {wall:%H:%M} ET)")
    out: dict[str, Any] = {"qty": qty, "notional": notional}
    if order_type in ("limit", "stop_limit"):
        if limit_price is None or limit_price <= 0:
            raise OrderConstraintError("limit orders need a positive limit_price")
        out["limit_price"] = round_to_tick(limit_price)
    elif limit_price is not None and order_class == "simple":
        raise OrderConstraintError(f"{order_type} orders do not take a limit_price")
    if order_type in ("stop", "stop_limit"):
        if stop_price is None or stop_price <= 0:
            raise OrderConstraintError("stop orders need a positive stop_price")
        out["stop_price"] = round_to_tick(stop_price)
    if order_class in ("oto", "bracket"):
        if tif not in ("day", "gtc"):
            raise OrderConstraintError("bracket/OTO orders must be DAY or GTC (auction TIFs are not supported for advanced classes)")
        if order_type not in ("market", "limit"):
            raise OrderConstraintError("bracket/OTO entries must be market or limit orders")
        base = limit_price if limit_price is not None else base_price
        if base is None or base <= 0:
            raise OrderConstraintError("bracket/OTO needs a base price (limit_price or base_price) to check the leg distances")
        if stop_price is None:
            raise OrderConstraintError("bracket/OTO needs a stop_price")
        sp = round_to_tick(stop_price)
        if side == "buy" and sp > base - 0.01 + 1e-9:
            raise OrderConstraintError(f"stop {sp} must be at least $0.01 below the base price {base} for a buy")
        if side == "sell" and sp < base + 0.01 - 1e-9:
            raise OrderConstraintError(f"stop {sp} must be at least $0.01 above the base price {base} for a sell")
        out["stop_price"] = sp
        if order_class == "bracket":
            if take_profit_price is None:
                raise OrderConstraintError("bracket needs a take_profit_price")
            tp = round_to_tick(take_profit_price)
            if side == "buy" and tp < base + 0.01 - 1e-9:
                raise OrderConstraintError(f"take profit {tp} must be at least $0.01 above the base price {base} for a buy")
            if side == "sell" and tp > base - 0.01 + 1e-9:
                raise OrderConstraintError(f"take profit {tp} must be at least $0.01 below the base price {base} for a sell")
            out["take_profit_price"] = tp
        if limit_price is not None:
            out["limit_price"] = round_to_tick(limit_price)
    return out


def can_replace(order) -> tuple[bool, str]:
    """Alpaca cannot replace notional orders or orders that are no longer open."""
    if getattr(order, "notional", None) is not None and getattr(order, "qty", 0) in (0, None):
        return False, "notional orders cannot be replaced"
    if getattr(order, "is_terminal", False):
        return False, f"order is {order.status}"
    return True, ""
