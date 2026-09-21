"""In-memory broker for tests and dry runs. Fills market orders on demand via ``fill_all``."""
from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

from bot.data.calendar import NY, SessionInfo, fallback_session
from bot.execution.broker import AccountInfo, BrokerPosition, ClockInfo, OrderInfo


class DuplicateClientOrderId(Exception):
    status_code = 422


class FakeBroker:
    name = "fake"
    is_paper = True

    def __init__(self, cash: float = 100_000.0, prices: dict[str, float] | None = None):
        self.cash = cash
        self.prices: dict[str, float] = dict(prices or {})
        self.positions: dict[str, int] = {}
        self.avg_price: dict[str, float] = {}
        self.orders: dict[str, OrderInfo] = {}
        self._ids = itertools.count(1)
        self.submitted: list[OrderInfo] = []
        self.now = datetime(2024, 1, 5, 20, 0, tzinfo=NY)

    # ---- test helpers
    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def fill_all(self) -> None:
        for cid, o in list(self.orders.items()):
            if o.status in ("new", "accepted"):
                px = self.prices[o.symbol]
                signed = o.qty if o.side == "buy" else -o.qty
                prev = self.positions.get(o.symbol, 0)
                new = prev + signed
                if new != 0 and (prev == 0 or (prev > 0) == (new > 0)) and abs(new) > abs(prev):
                    self.avg_price[o.symbol] = (abs(prev) * self.avg_price.get(o.symbol, px) + abs(signed) * px) / abs(new)
                elif new != 0 and prev == 0:
                    self.avg_price[o.symbol] = px
                self.positions[o.symbol] = new
                if new == 0:
                    self.positions.pop(o.symbol, None)
                    self.avg_price.pop(o.symbol, None)
                self.cash -= signed * px
                self.orders[cid] = OrderInfo(o.id, cid, o.symbol, o.side, o.qty, "filled", o.qty, px, o.submitted_at, self.now)

    # ---- Broker protocol
    def get_account(self) -> AccountInfo:
        eq = self.cash + sum(q * self.prices[s] for s, q in self.positions.items())
        return AccountInfo(eq, self.cash, self.cash * 2)

    def get_positions(self) -> dict[str, BrokerPosition]:
        return {s: BrokerPosition(s, q, self.avg_price.get(s, self.prices[s]), q * self.prices[s], self.prices[s])
                for s, q in self.positions.items()}

    def get_open_orders(self) -> list[OrderInfo]:
        return [o for o in self.orders.values() if not o.is_terminal]

    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        return self.orders.get(client_order_id)

    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str, tif: str = "opg") -> OrderInfo:
        if client_order_id in self.orders:
            raise DuplicateClientOrderId(f"client_order_id {client_order_id} already exists")
        o = OrderInfo(str(next(self._ids)), client_order_id, symbol, side, int(qty), "new", 0, None, self.now, None)
        self.orders[client_order_id] = o
        self.submitted.append(o)
        return o

    def close_all_positions(self) -> None:
        for s, q in list(self.positions.items()):
            cid = f"close-all-{s}-{next(self._ids)}"
            self.submit_market_order(s, abs(q), "sell" if q > 0 else "buy", cid, "day")
        self.fill_all()

    def cancel_all_orders(self) -> None:
        for cid, o in list(self.orders.items()):
            if not o.is_terminal:
                self.orders[cid] = OrderInfo(o.id, cid, o.symbol, o.side, o.qty, "canceled", 0, None, o.submitted_at, None)

    def get_clock(self) -> ClockInfo:
        s = fallback_session(self.now.date())
        is_open = bool(s and s.open <= self.now < s.close)
        return ClockInfo(self.now, is_open, self.now + timedelta(hours=12), self.now + timedelta(hours=18))

    def get_sessions(self, start: date, end: date) -> list[SessionInfo]:
        out, d = [], start
        while d <= end:
            s = fallback_session(d)
            if s:
                out.append(s)
            d += timedelta(days=1)
        return out
