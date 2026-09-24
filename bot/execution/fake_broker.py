"""In-memory broker for tests and dry runs.

Deterministic and inspectable. Fills happen only when the test calls ``fill_all`` / ``fill``; failures are
injected with ``fail_next`` (any exception, e.g. a fake 429/500/connection error), ``reject_next`` (order ends
``rejected``), ``partial_fill`` and ``cancel``. Fractional quantities and asset metadata are supported so the
Phase 5 constraints can be unit-tested.
"""
from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

from bot.data.calendar import NY, SessionInfo, fallback_session
from bot.execution.broker import AccountInfo, AssetInfo, BrokerPosition, ClockInfo, OrderInfo, QuoteInfo


class FakeAPIError(Exception):
    """Mimics alpaca.common.exceptions.APIError: has status_code and a message."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class DuplicateClientOrderId(FakeAPIError):
    def __init__(self, cid: str):
        super().__init__(f"client_order_id {cid} already exists", 422)


class FakeBroker:
    name = "fake"

    def __init__(self, cash: float = 100_000.0, prices: dict[str, float] | None = None, *, env: str = "paper",
                 account_number: str | None = None, assets: dict[str, AssetInfo] | None = None):
        self.env = env
        self.is_paper = env == "paper"
        self.cash = cash
        self.prices: dict[str, float] = dict(prices or {})
        self.positions: dict[str, float] = {}
        self.avg_price: dict[str, float] = {}
        self.orders: dict[str, OrderInfo] = {}
        self._ids = itertools.count(1)
        self.submitted: list[OrderInfo] = []
        self.now = datetime(2024, 1, 5, 20, 0, tzinfo=NY)
        self.account_number = account_number or ("PA0000001" if self.is_paper else "900000001")
        self.status = "ACTIVE"
        self.trading_blocked = False
        self.account_blocked = False
        self.shorting_enabled = False
        self.assets = dict(assets or {})
        self.quote_age = timedelta(seconds=5)
        self.spread_bps = 2.0
        self._fail_queue: list[Exception] = []
        self._reject_next: list[str] = []
        self.last_request_id: str | None = "fake-req-0"
        self.calls: list[str] = []
        self.market_open_override: bool | None = None

    # ------------------------------------------------------------- test hooks
    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def fail_next(self, exc: Exception, times: int = 1) -> None:
        self._fail_queue.extend([exc] * times)

    def reject_next(self, reason: str = "insufficient buying power") -> None:
        self._reject_next.append(reason)

    def _maybe_fail(self, what: str) -> None:
        self.calls.append(what)
        self.last_request_id = f"fake-req-{len(self.calls)}"
        if self._fail_queue:
            raise self._fail_queue.pop(0)

    def fill(self, client_order_id: str, qty: float | None = None, price: float | None = None) -> None:
        o = self.orders[client_order_id]
        if o.is_terminal:
            return
        px = price if price is not None else self.prices[o.symbol]
        fill_qty = o.qty - o.filled_qty if qty is None else min(qty, o.qty - o.filled_qty)
        signed = fill_qty if o.side == "buy" else -fill_qty
        prev = self.positions.get(o.symbol, 0.0)
        new = prev + signed
        if new != 0 and (prev == 0 or (prev > 0) == (new > 0)) and abs(new) > abs(prev):
            self.avg_price[o.symbol] = (abs(prev) * self.avg_price.get(o.symbol, px) + abs(signed) * px) / abs(new)
        if abs(new) < 1e-9:
            self.positions.pop(o.symbol, None)
            self.avg_price.pop(o.symbol, None)
        else:
            self.positions[o.symbol] = new
        self.cash -= signed * px
        total_filled = o.filled_qty + fill_qty
        prev_notional = (o.filled_avg_price or 0) * o.filled_qty
        avg = (prev_notional + fill_qty * px) / total_filled
        status = "filled" if abs(total_filled - o.qty) < 1e-9 else "partially_filled"
        self.orders[client_order_id] = OrderInfo(o.id, client_order_id, o.symbol, o.side, o.qty, status, total_filled, avg,
                                                  o.submitted_at, self.now if status == "filled" else None, o.notional, o.time_in_force)

    def partial_fill(self, client_order_id: str, qty: float, price: float | None = None) -> None:
        self.fill(client_order_id, qty=qty, price=price)

    def fill_all(self) -> None:
        for cid, o in list(self.orders.items()):
            if o.is_open:
                self.fill(cid)

    def cancel(self, client_order_id: str) -> None:
        o = self.orders[client_order_id]
        if not o.is_terminal:
            status = "canceled"
            self.orders[client_order_id] = OrderInfo(o.id, client_order_id, o.symbol, o.side, o.qty, status, o.filled_qty,
                                                      o.filled_avg_price, o.submitted_at, None, o.notional, o.time_in_force)

    # ---------------------------------------------------------- Broker protocol
    def get_account(self) -> AccountInfo:
        self._maybe_fail("get_account")
        long_mv = sum(q * self.prices[s] for s, q in self.positions.items() if q > 0)
        short_mv = sum(q * self.prices[s] for s, q in self.positions.items() if q < 0)
        eq = self.cash + long_mv + short_mv
        return AccountInfo(equity=eq, cash=self.cash, buying_power=max(self.cash, 0.0), account_number=self.account_number,
                           status=self.status, trading_blocked=self.trading_blocked, account_blocked=self.account_blocked,
                           shorting_enabled=self.shorting_enabled, last_equity=eq, long_market_value=long_mv, short_market_value=short_mv)

    def verify_account_env(self) -> tuple[bool, str]:
        a = self.get_account()
        if self.is_paper != a.is_paper_account_number:
            return False, "account number shape does not match env"
        return True, "ok"

    def get_positions(self) -> dict[str, BrokerPosition]:
        self._maybe_fail("get_positions")
        return {s: BrokerPosition(s, q, self.avg_price.get(s, self.prices[s]), q * self.prices[s], self.prices[s], abs(q))
                for s, q in self.positions.items()}

    def get_open_orders(self) -> list[OrderInfo]:
        self._maybe_fail("get_open_orders")
        return [o for o in self.orders.values() if o.is_open]

    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        self._maybe_fail("get_order_by_client_id")
        return self.orders.get(client_order_id)

    def get_order_by_id(self, order_id: str) -> OrderInfo | None:
        self._maybe_fail("get_order_by_id")
        return next((o for o in self.orders.values() if o.id == order_id), None)

    def submit_market_order(self, symbol: str, qty: float, side: str, client_order_id: str, tif: str = "opg") -> OrderInfo:
        self._maybe_fail("submit_market_order")
        if client_order_id in self.orders:
            raise DuplicateClientOrderId(client_order_id)
        if symbol not in self.prices:
            raise FakeAPIError(f"asset {symbol} not found", 422)
        q = float(qty)
        if q <= 0:
            raise FakeAPIError("qty must be > 0", 422)
        if q != int(q) and tif != "day":
            raise FakeAPIError("fractional orders must be DAY orders", 422)
        asset = self.assets.get(symbol)
        if q != int(q) and asset is not None and not asset.fractionable:
            raise FakeAPIError(f"{symbol} is not fractionable", 422)
        status = "new"
        if self._reject_next:
            status, reason = "rejected", self._reject_next.pop(0)
        elif side == "buy" and q * self.prices[symbol] > self.cash + 1e-9:
            status = "rejected"   # insufficient buying power (Alpaca actually returns HTTP 403; we model the terminal state)
        o = OrderInfo(str(next(self._ids)), client_order_id, symbol, side, q, status, 0.0, None, self.now, None, None, tif)
        self.orders[client_order_id] = o
        self.submitted.append(o)
        return o

    def cancel_order(self, order_id: str) -> None:
        self._maybe_fail("cancel_order")
        for cid, o in self.orders.items():
            if o.id == order_id:
                self.cancel(cid)

    def close_all_positions(self) -> None:
        self._maybe_fail("close_all_positions")
        for s, q in list(self.positions.items()):
            cid = f"close-all-{s}-{next(self._ids)}"
            self.submit_market_order(s, abs(q), "sell" if q > 0 else "buy", cid, "day")
        self.fill_all()

    def cancel_all_orders(self) -> None:
        self._maybe_fail("cancel_all_orders")
        for cid in list(self.orders):
            self.cancel(cid)

    def get_clock(self) -> ClockInfo:
        self._maybe_fail("get_clock")
        s = fallback_session(self.now.date())
        is_open = bool(s and s.open <= self.now < s.close)
        if self.market_open_override is not None:
            is_open = self.market_open_override
        return ClockInfo(self.now, is_open, self.now + timedelta(hours=12), self.now + timedelta(hours=18))

    def get_sessions(self, start: date, end: date) -> list[SessionInfo]:
        self._maybe_fail("get_sessions")
        out, d = [], start
        while d <= end:
            s = fallback_session(d)
            if s:
                out.append(s)
            d += timedelta(days=1)
        return out

    def get_asset(self, symbol: str) -> AssetInfo:
        self._maybe_fail("get_asset")
        if symbol in self.assets:
            return self.assets[symbol]
        if symbol not in self.prices:
            raise FakeAPIError(f"asset {symbol} not found", 404)
        return AssetInfo(symbol, tradable=True, fractionable=True, shortable=True, marginable=True, easy_to_borrow=True)

    def get_latest_quote(self, symbol: str) -> QuoteInfo | None:
        self._maybe_fail("get_latest_quote")
        if symbol not in self.prices:
            return None
        p = self.prices[symbol]
        half = p * self.spread_bps / 2e4
        return QuoteInfo(symbol, self.now - self.quote_age, p - half, p + half, 100, 100)
