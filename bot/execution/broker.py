"""Broker abstraction + Alpaca implementation.

Construction is explicit about the environment: ``AlpacaBroker.for_env(settings, "paper")`` or ``"live"``.
The two environments use separate key pairs and separate hosts, and the constructor verifies the SDK's base URL
matches the requested environment in BOTH directions. ``verify_account_env()`` additionally checks the account
number shape returned by the account endpoint (paper accounts are prefixed ``PA``).

Every call goes through ``with_retry`` (exponential backoff on 429/5xx/network). Duplicate ``client_order_id``
submissions are rejected by Alpaca, which is our last line of defence against double orders; the first line is
the state file. Alpaca request ids (``X-Request-ID`` response header) are captured for every call and exposed
via ``last_request_id`` so decision records can reference them.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from bot.config import Settings, TradingEnv
from bot.data.calendar import NY, SessionInfo, normalize_session_time
from bot.utils.retry import with_retry

log = logging.getLogger(__name__)

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}
OPEN_STATES = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding", "held", "pending_cancel", "pending_replace", "calculated"}
_REQ_ID_RE = re.compile(r"request.?id", re.IGNORECASE)


@dataclass(frozen=True)
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    currency: str = "USD"
    account_number: str = ""
    status: str = ""
    trading_blocked: bool = False
    account_blocked: bool = False
    transfers_blocked: bool = False
    trade_suspended_by_user: bool = False
    shorting_enabled: bool = False
    pattern_day_trader: bool = False
    daytrade_count: int = 0
    multiplier: float = 1.0
    last_equity: float = 0.0
    long_market_value: float = 0.0
    short_market_value: float = 0.0

    @property
    def is_paper_account_number(self) -> bool:
        return self.account_number.upper().startswith("PA")

    @property
    def healthy(self) -> bool:
        return (self.status.upper() == "ACTIVE" and not self.trading_blocked and not self.account_blocked
                and not self.trade_suspended_by_user)


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float               # signed: negative = short
    avg_entry_price: float
    market_value: float
    current_price: float
    qty_available: float | None = None


@dataclass(frozen=True)
class OrderInfo:
    id: str
    client_order_id: str
    symbol: str
    side: str              # "buy" | "sell"
    qty: float
    status: str
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None
    notional: float | None = None
    time_in_force: str = ""
    order_type: str = "market"
    limit_price: float | None = None
    stop_price: float | None = None
    order_class: str = "simple"
    legs: tuple = ()                    # nested legs of a bracket/OTO (OrderInfo)
    updated_at: datetime | None = None
    extended_hours: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES


@dataclass(frozen=True)
class AssetInfo:
    symbol: str
    tradable: bool
    fractionable: bool
    shortable: bool
    marginable: bool
    easy_to_borrow: bool
    asset_class: str = "us_equity"
    status: str = "active"
    exchange: str = ""


@dataclass(frozen=True)
class QuoteInfo:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.ask or self.bid

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m * 1e4 if m > 0 and self.bid > 0 and self.ask > 0 else float("inf")


@dataclass(frozen=True)
class ClockInfo:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class Broker(Protocol):
    name: str
    env: TradingEnv
    is_paper: bool
    last_request_id: str | None

    def get_account(self) -> AccountInfo: ...
    def get_positions(self) -> dict[str, BrokerPosition]: ...
    def get_open_orders(self) -> list[OrderInfo]: ...
    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None: ...
    def get_order_by_id(self, order_id: str) -> OrderInfo | None: ...
    def submit_market_order(self, symbol: str, qty: float, side: str, client_order_id: str, tif: str) -> OrderInfo: ...
    def cancel_order(self, order_id: str) -> None: ...
    def close_all_positions(self) -> None: ...
    def cancel_all_orders(self) -> None: ...
    def get_clock(self) -> ClockInfo: ...
    def get_sessions(self, start: date, end: date) -> list[SessionInfo]: ...
    def get_asset(self, symbol: str) -> AssetInfo: ...
    def get_latest_quote(self, symbol: str) -> QuoteInfo | None: ...


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, settings: Settings, *, env: TradingEnv):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        if env not in ("paper", "live"):
            raise ValueError(f"env must be 'paper' or 'live', got {env!r}")
        key, secret = settings.credentials(env)   # raises if missing or wrong-shaped
        self.env: TradingEnv = env
        self.is_paper = env == "paper"
        self.settings = settings
        self.client = TradingClient(api_key=key, secret_key=secret, paper=self.is_paper)
        self.data = StockHistoricalDataClient(api_key=key, secret_key=secret)
        # Belt and braces: the SDK host must match the requested environment, in both directions.
        base = getattr(self.client, "_base_url", "")
        base = str(getattr(base, "value", base))
        self.base_url = base
        if self.is_paper and "paper-api" not in base:
            raise RuntimeError(f"paper env requested but trading client base URL is {base!r}")
        if not self.is_paper and "paper-api" in base:
            raise RuntimeError(f"live env requested but trading client base URL is {base!r}")
        self.last_request_id: str | None = None
        self.request_ids: deque[tuple[str, str]] = deque(maxlen=200)
        for c in (self.client, self.data):
            sess = getattr(c, "_session", None)
            if sess is not None:
                sess.hooks.setdefault("response", []).append(self._capture_request_id)
        del key, secret

    @classmethod
    def for_env(cls, settings: Settings, env: TradingEnv) -> AlpacaBroker:
        return cls(settings, env=env)

    def _capture_request_id(self, resp, *args, **kwargs) -> None:
        for h, v in resp.headers.items():
            if _REQ_ID_RE.search(h):
                self.last_request_id = v
                self.request_ids.append((resp.request.url.split("?")[0], v))
                return

    # ---------------------------------------------------------------- reads
    def get_account(self) -> AccountInfo:
        a = with_retry(self.client.get_account, what="get_account")
        status = a.status.value if hasattr(a.status, "value") else str(a.status)
        return AccountInfo(
            equity=float(a.equity or 0), cash=float(a.cash or 0), buying_power=float(a.buying_power or 0),
            currency=str(a.currency or "USD"), account_number=str(a.account_number or ""), status=str(status),
            trading_blocked=bool(a.trading_blocked), account_blocked=bool(a.account_blocked),
            transfers_blocked=bool(a.transfers_blocked), trade_suspended_by_user=bool(a.trade_suspended_by_user),
            shorting_enabled=bool(a.shorting_enabled), pattern_day_trader=bool(a.pattern_day_trader),
            daytrade_count=int(a.daytrade_count or 0), multiplier=float(a.multiplier or 1),
            last_equity=float(a.last_equity or 0), long_market_value=float(a.long_market_value or 0),
            short_market_value=float(a.short_market_value or 0),
        )

    def verify_account_env(self) -> tuple[bool, str]:
        """Ask the account endpoint and check its account-number shape against the requested env."""
        acct = self.get_account()
        looks_paper = acct.is_paper_account_number
        if self.is_paper and not looks_paper:
            return False, f"paper env but account number does not start with PA (got {acct.account_number[:2]}…)"
        if not self.is_paper and looks_paper:
            return False, "live env but account number starts with PA (paper account)"
        return True, f"account number shape matches {self.env}"

    def get_positions(self) -> dict[str, BrokerPosition]:
        out = {}
        for p in with_retry(self.client.get_all_positions, what="get_all_positions"):
            qty = float(p.qty)
            if str(p.side).lower().endswith("short") and qty > 0:
                qty = -qty
            out[p.symbol] = BrokerPosition(p.symbol, qty, float(p.avg_entry_price), float(p.market_value or 0),
                                           float(p.current_price or 0),
                                           float(p.qty_available) if p.qty_available is not None else None)
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

    def get_order_by_id(self, order_id: str) -> OrderInfo | None:
        from alpaca.common.exceptions import APIError

        try:
            o = with_retry(lambda: self.client.get_order_by_id(order_id), what="get_order_by_id")
        except APIError as e:
            if e.status_code == 404:
                return None
            raise
        return self._order(o)

    def get_asset(self, symbol: str) -> AssetInfo:
        a = with_retry(lambda: self.client.get_asset(symbol), what=f"get_asset({symbol})")
        return AssetInfo(symbol=str(a.symbol), tradable=bool(a.tradable), fractionable=bool(a.fractionable),
                         shortable=bool(a.shortable), marginable=bool(a.marginable), easy_to_borrow=bool(a.easy_to_borrow),
                         asset_class=str(a.asset_class.value if hasattr(a.asset_class, "value") else a.asset_class),
                         status=str(a.status.value if hasattr(a.status, "value") else a.status),
                         exchange=str(a.exchange.value if hasattr(a.exchange, "value") else a.exchange))

    def get_latest_quote(self, symbol: str) -> QuoteInfo | None:
        from alpaca.common.exceptions import APIError
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest

        def _call(feed: DataFeed):
            return self.data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=feed))

        feed = DataFeed(self.settings.data_feed)
        try:
            res = with_retry(lambda: _call(feed), what=f"latest_quote({symbol})")
        except APIError as e:
            if feed == DataFeed.SIP and "subscription" in str(e).lower():
                res = with_retry(lambda: _call(DataFeed.IEX), what=f"latest_quote({symbol},iex)")
            else:
                raise
        q = res.get(symbol) if isinstance(res, dict) else None
        if q is None:
            return None
        ts = q.timestamp.astimezone(NY) if q.timestamp.tzinfo else q.timestamp.replace(tzinfo=NY)
        return QuoteInfo(symbol, ts, float(q.bid_price or 0), float(q.ask_price or 0), float(q.bid_size or 0), float(q.ask_size or 0))

    # --------------------------------------------------------------- writes
    def _now_et(self) -> datetime | None:
        """Broker clock for the OPG/CLS acceptance windows (cached 5 s). None when unavailable: the window check is
        then skipped and the API decides."""
        cached = getattr(self, "_clock_cache", None)
        import time as _time
        if cached and _time.monotonic() - cached[0] < 5:
            return cached[1]
        try:
            ts = self.get_clock().timestamp
        except Exception as e:  # noqa: BLE001
            log.warning("clock unavailable for order window check (%s); relying on the API", type(e).__name__)
            return None
        self._clock_cache = (_time.monotonic(), ts)
        return ts

    def _submit(self, req, client_order_id: str, symbol: str) -> OrderInfo:
        from alpaca.common.exceptions import APIError
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

    @staticmethod
    def _qty(q: float):
        return int(q) if q == int(q) else q

    def submit_market_order(self, symbol: str, qty: float, side: str, client_order_id: str, tif: str = "opg") -> OrderInfo:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        from bot.execution.constraints import validate_order

        q = float(qty)
        validate_order(symbol=symbol, qty=q, side=side, order_type="market", tif=tif, now=self._now_et())
        req = MarketOrderRequest(symbol=symbol, qty=self._qty(q), side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                 time_in_force=TimeInForce(tif), client_order_id=client_order_id)
        return self._submit(req, client_order_id, symbol)

    def submit_limit_order(self, symbol: str, qty: float, side: str, limit_price: float, client_order_id: str, tif: str = "day",
                           extended_hours: bool = False) -> OrderInfo:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        from bot.execution.constraints import validate_order

        q = float(qty)
        v = validate_order(symbol=symbol, qty=q, side=side, order_type="limit", tif=tif, limit_price=limit_price, extended_hours=extended_hours, now=self._now_et())
        req = LimitOrderRequest(symbol=symbol, qty=self._qty(q), side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce(tif),
                                limit_price=v["limit_price"], extended_hours=extended_hours or None, client_order_id=client_order_id)
        return self._submit(req, client_order_id, symbol)

    def submit_stop_order(self, symbol: str, qty: float, side: str, stop_price: float, client_order_id: str, tif: str = "gtc") -> OrderInfo:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        from bot.execution.constraints import validate_order

        q = float(qty)
        v = validate_order(symbol=symbol, qty=q, side=side, order_type="stop", tif=tif, stop_price=stop_price, now=self._now_et())
        req = StopOrderRequest(symbol=symbol, qty=self._qty(q), side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce(tif),
                               stop_price=v["stop_price"], client_order_id=client_order_id)
        return self._submit(req, client_order_id, symbol)

    def submit_oto(self, symbol: str, qty: float, side: str, client_order_id: str, *, stop_price: float, entry_type: str = "market",
                   limit_price: float | None = None, tif: str = "day", base_price: float | None = None) -> OrderInfo:
        """Entry + stop-loss leg (order_class OTO). Whole shares, DAY/GTC only; stop >= $0.01 from the base price."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopLossRequest
        from bot.execution.constraints import validate_order

        q = float(qty)
        v = validate_order(symbol=symbol, qty=q, side=side, order_type=entry_type, tif=tif, limit_price=limit_price, stop_price=stop_price,
                           order_class="oto", base_price=base_price, now=self._now_et())
        common = dict(symbol=symbol, qty=self._qty(q), side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce(tif),
                      order_class=OrderClass.OTO, stop_loss=StopLossRequest(stop_price=v["stop_price"]), client_order_id=client_order_id)
        req = LimitOrderRequest(limit_price=v["limit_price"], **common) if entry_type == "limit" else MarketOrderRequest(**common)
        return self._submit(req, client_order_id, symbol)

    def submit_bracket(self, symbol: str, qty: float, side: str, client_order_id: str, *, take_profit_price: float, stop_price: float,
                       entry_type: str = "market", limit_price: float | None = None, tif: str = "day", base_price: float | None = None) -> OrderInfo:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest
        from bot.execution.constraints import validate_order

        q = float(qty)
        v = validate_order(symbol=symbol, qty=q, side=side, order_type=entry_type, tif=tif, limit_price=limit_price, stop_price=stop_price,
                           take_profit_price=take_profit_price, order_class="bracket", base_price=base_price, now=self._now_et())
        common = dict(symbol=symbol, qty=self._qty(q), side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce(tif),
                      order_class=OrderClass.BRACKET, stop_loss=StopLossRequest(stop_price=v["stop_price"]),
                      take_profit=TakeProfitRequest(limit_price=v["take_profit_price"]), client_order_id=client_order_id)
        req = LimitOrderRequest(limit_price=v["limit_price"], **common) if entry_type == "limit" else MarketOrderRequest(**common)
        return self._submit(req, client_order_id, symbol)

    def replace_order(self, order_id: str, *, qty: float | None = None, limit_price: float | None = None, stop_price: float | None = None,
                      tif: str | None = None) -> OrderInfo:
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import ReplaceOrderRequest
        from bot.execution.constraints import OrderConstraintError, can_replace, round_to_tick

        existing = self.get_order_by_id(order_id)
        if existing is None:
            raise OrderConstraintError(f"order {order_id} not found; nothing to replace")
        ok, why = can_replace(existing)
        if not ok:
            raise OrderConstraintError(why)
        if qty is not None and not float(qty).is_integer():
            raise OrderConstraintError("replace supports whole-share quantities only")
        req = ReplaceOrderRequest(qty=int(qty) if qty is not None else None, time_in_force=TimeInForce(tif) if tif else None,
                                  limit_price=round_to_tick(limit_price) if limit_price is not None else None,
                                  stop_price=round_to_tick(stop_price) if stop_price is not None else None)
        o = with_retry(lambda: self.client.replace_order_by_id(order_id, req), what="replace_order")
        return self._order(o)

    def get_orders_since(self, after: datetime, status: str = "all", *, nested: bool = True, limit: int = 500) -> list[OrderInfo]:
        """Orders submitted after ``after`` (ascending), nested legs included. Used to reconcile after a reconnect/restart."""
        from alpaca.common.enums import Sort
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus(status), after=after, nested=nested, limit=limit, direction=Sort.ASC)
        orders = with_retry(lambda: self.client.get_orders(req), what="get_orders_since")
        return [self._order(o) for o in orders]

    def cancel_order(self, order_id: str) -> None:
        with_retry(lambda: self.client.cancel_order_by_id(order_id), what="cancel_order")

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
        return [self.session_from_calendar(d) for d in days]

    @staticmethod
    def session_from_calendar(d) -> SessionInfo:
        """Normalisation boundary for alpaca-py ``Calendar`` objects (open/close may be time, naive or aware datetime)."""
        session_date = d.date if isinstance(d.date, date) else datetime.fromisoformat(str(d.date)).date()
        o = normalize_session_time(session_date, d.open)
        c = normalize_session_time(session_date, d.close)
        if c <= o:
            raise ValueError(f"calendar entry {session_date}: close {c} is not after open {o}")
        return SessionInfo(session_date, o, c, "alpaca")

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _order(o) -> OrderInfo:
        def ev(x):
            return str(x.value if hasattr(x, "value") else x).lower()
        return OrderInfo(
            id=str(o.id), client_order_id=str(o.client_order_id), symbol=str(o.symbol), side=ev(o.side),
            qty=float(o.qty or 0), status=ev(o.status), filled_qty=float(o.filled_qty or 0),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price is not None else None,
            submitted_at=o.submitted_at, filled_at=o.filled_at,
            notional=float(o.notional) if o.notional is not None else None,
            time_in_force=ev(o.time_in_force), order_type=ev(o.order_type or o.type),
            limit_price=float(o.limit_price) if getattr(o, "limit_price", None) is not None else None,
            stop_price=float(o.stop_price) if getattr(o, "stop_price", None) is not None else None,
            order_class=ev(getattr(o, "order_class", None) or "simple"),
            legs=tuple(AlpacaBroker._order(leg) for leg in (getattr(o, "legs", None) or [])),
            updated_at=getattr(o, "updated_at", None), extended_hours=bool(getattr(o, "extended_hours", False) or False),
        )
