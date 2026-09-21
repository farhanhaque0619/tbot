"""Broker abstraction + Alpaca implementation.

Every call goes through ``with_retry`` (exponential backoff on 429/5xx/network).
Duplicate ``client_order_id`` submissions are rejected by Alpaca, which is our
last line of defence against double orders; the first line is the state file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from bot.config import Settings
from bot.data.calendar import NY, SessionInfo
from bot.utils.retry import with_retry

log = logging.getLogger(__name__)

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}


@dataclass(frozen=True)
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    currency: str = "USD"


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int               # signed: negative = short
    avg_entry_price: float
    market_value: float
    current_price: float


@dataclass(frozen=True)
class OrderInfo:
    id: str
    client_order_id: str
    symbol: str
    side: str              # "buy" | "sell"
    qty: int
    status: str
    filled_qty: int
    filled_avg_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"


@dataclass(frozen=True)
class ClockInfo:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class Broker(Protocol):
    name: str
    is_paper: bool

    def get_account(self) -> AccountInfo: ...
    def get_positions(self) -> dict[str, BrokerPosition]: ...
    def get_open_orders(self) -> list[OrderInfo]: ...
    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None: ...
    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str, tif: str) -> OrderInfo: ...
    def close_all_positions(self) -> None: ...
    def cancel_all_orders(self) -> None: ...
    def get_clock(self) -> ClockInfo: ...
    def get_sessions(self, start: date, end: date) -> list[SessionInfo]: ...


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, settings: Settings, *, paper: bool = True):
        from alpaca.trading.client import TradingClient

        if not settings.has_alpaca_keys:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (see .env.example)")
        self.is_paper = paper
        self.client = TradingClient(
            api_key=settings.alpaca_api_key.get_secret_value(),
            secret_key=settings.alpaca_secret_key.get_secret_value(),
            paper=paper,
        )
        if paper:
            # Belt and braces: refuse to talk to anything but the paper host in paper mode.
            base = str(getattr(self.client, "_base_url", ""))
            if base and "paper-api" not in base:
                raise RuntimeError(f"paper mode but trading client base URL is {base!r}")

    # ---------------------------------------------------------------- reads
    def get_account(self) -> AccountInfo:
        a = with_retry(self.client.get_account, what="get_account")
        return AccountInfo(float(a.equity), float(a.cash), float(a.buying_power), str(a.currency or "USD"))

    def get_positions(self) -> dict[str, BrokerPosition]:
        out = {}
        for p in with_retry(self.client.get_all_positions, what="get_all_positions"):
            qty = int(float(p.qty))
            if str(p.side).lower().endswith("short") and qty > 0:
                qty = -qty
            out[p.symbol] = BrokerPosition(p.symbol, qty, float(p.avg_entry_price), float(p.market_value or 0),
                                           float(p.current_price or 0))
        return out

    def get_open_orders(self) -> list[OrderInfo]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = with_retry(lambda: self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)),
                            what="get_orders")
        return [self._order(o) for o in orders]

    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        from alpaca.common.exceptions import APIError

        try:
            o = with_retry(lambda: self.client.get_order_by_client_id(client_order_id), what="get_order_by_client_id")
        except APIError as e:
            if e.status_code == 404:
                return None
            raise
        return self._order(o)

    # --------------------------------------------------------------- writes
    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str, tif: str = "opg") -> OrderInfo:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                 time_in_force=TimeInForce(tif), client_order_id=client_order_id)
        try:
            o = with_retry(lambda: self.client.submit_order(req), what=f"submit_order({symbol})")
        except APIError as e:
            # Duplicate client_order_id -> the order already exists (e.g. we crashed after submitting).
            existing = self.get_order_by_client_id(client_order_id) if e.status_code in (400, 422) else None
            if existing is not None:
                log.warning("order %s already exists at broker (status %s); not resubmitting", client_order_id, existing.status)
                return existing
            raise
        return self._order(o)

    def close_all_positions(self) -> None:
        with_retry(lambda: self.client.close_all_positions(cancel_orders=True), what="close_all_positions")

    def cancel_all_orders(self) -> None:
        with_retry(self.client.cancel_orders, what="cancel_orders")

    # -------------------------------------------------------------- calendar
    def get_clock(self) -> ClockInfo:
        c = with_retry(self.client.get_clock, what="get_clock")
        return ClockInfo(c.timestamp.astimezone(NY), bool(c.is_open), c.next_open.astimezone(NY), c.next_close.astimezone(NY))

    def get_sessions(self, start: date, end: date) -> list[SessionInfo]:
        from alpaca.trading.requests import GetCalendarRequest

        days = with_retry(lambda: self.client.get_calendar(GetCalendarRequest(start=start, end=end)), what="get_calendar")
        out = []
        for d in days:
            o = datetime.combine(d.date, d.open, NY)
            c = datetime.combine(d.date, d.close, NY)
            out.append(SessionInfo(d.date, o, c, "alpaca"))
        return out

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _order(o) -> OrderInfo:
        return OrderInfo(
            id=str(o.id), client_order_id=str(o.client_order_id), symbol=str(o.symbol),
            side=str(o.side.value if hasattr(o.side, "value") else o.side).lower(),
            qty=int(float(o.qty or 0)), status=str(o.status.value if hasattr(o.status, "value") else o.status).lower(),
            filled_qty=int(float(o.filled_qty or 0)),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price is not None else None,
            submitted_at=o.submitted_at, filled_at=o.filled_at,
        )
