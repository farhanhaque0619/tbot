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
from bot.data.calendar import NY, SessionInfo
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
    def submit_market_order(self, symbol: str, qty: float, side: str, client_order_id: str, tif: str = "opg") -> OrderInfo:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        q = float(qty)
        if q <= 0:
            raise ValueError("qty must be positive")
        if q != int(q) and tif != "day":
            raise ValueError("fractional quantities require time_in_force=day at Alpaca")
        req = MarketOrderRequest(symbol=symbol, qty=int(q) if q == int(q) else q,
                                 side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
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
        out = []
        for d in days:
            o = datetime.combine(d.date, d.open, NY)
            c = datetime.combine(d.date, d.close, NY)
            out.append(SessionInfo(d.date, o, c, "alpaca"))
        return out

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
        )
