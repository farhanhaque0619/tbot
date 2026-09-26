"""SimBroker: the fake broker driven by minute bars and auctions (Phase 2).

Order types: market, limit, stop; TIF day / gtc / opg / cls; order_class simple / oto (entry + stop leg) / bracket
(entry + take-profit + stop). Legs become live when the parent fills. Fills are produced by ``FillEngine`` from the
NEXT bar the engine feeds via ``step(bar)``; auction orders fill in ``session_open`` / ``session_close``.
Trade updates are delivered to a callback in the same shape the live trade_updates stream produces.
"""
from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from bot.backtest.fills import FillEngine, FillParams
from bot.core.events import BarEvent, TradeUpdateEvent
from bot.execution.broker import AccountInfo, AssetInfo, BrokerPosition, OrderInfo, QuoteInfo

log = logging.getLogger(__name__)
OPEN = {"new", "accepted", "partially_filled", "held"}


@dataclass
class SimOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str                      # buy | sell
    qty: float
    order_type: str                # market | limit | stop
    tif: str                       # day | gtc | opg | cls
    limit_price: float | None = None
    stop_price: float | None = None
    order_class: str = "simple"    # simple | oto | bracket
    legs: list["SimOrder"] = field(default_factory=list)
    status: str = "new"
    filled_qty: float = 0.0
    filled_notional: float = 0.0
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    parent_id: str | None = None
    held: bool = False             # legs wait for the parent fill
    session_date: Any = None
    elected: bool = False          # stop elected, fills on the next bar
    decision_ts: datetime | None = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1

    @property
    def avg_price(self) -> float | None:
        return self.filled_notional / self.filled_qty if self.filled_qty > 0 else None

    def info(self) -> OrderInfo:
        return OrderInfo(self.id, self.client_order_id, self.symbol, self.side, self.qty, self.status, self.filled_qty,
                         self.avg_price, self.submitted_at, self.filled_at, None, self.tif, self.order_type)


class SimBroker:
    name = "sim"
    env = "paper"
    is_paper = True
    base_url = "sim://paper-api"

    def __init__(self, cash: float = 100_000.0, *, fills: FillEngine | None = None, symbol_kinds: dict[str, str] | None = None,
                 on_trade_update: Callable[[TradeUpdateEvent], None] | None = None, account_number: str = "PA-SIM",
                 whole_share_only: bool = False):
        self.cash = cash
        self.fills = fills or FillEngine(FillParams())
        self.symbol_kinds = dict(symbol_kinds or {})
        self.on_trade_update = on_trade_update
        self.account_number = account_number
        self.positions: dict[str, float] = {}
        self.avg_price: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self.last_quote_spread: dict[str, float] = {}
        self.orders: dict[str, SimOrder] = {}            # by client id
        self.by_id: dict[str, SimOrder] = {}
        self._ids = itertools.count(1)
        self.now: datetime | None = None
        self.costs_paid = 0.0
        self.traded_notional = 0.0
        self.fills_log: list[dict[str, Any]] = []
        self.whole_share_only = whole_share_only
        self.last_request_id: str | None = "sim-0"
        self.shorting_enabled = True
        self._peak = cash

    # ------------------------------------------------------------ accounting
    def equity(self) -> float:
        return self.cash + sum(q * self.last_price.get(s, self.avg_price.get(s, 0.0)) for s, q in self.positions.items())

    def get_account(self) -> AccountInfo:
        eq = self.equity()
        long_mv = sum(q * self.last_price.get(s, 0.0) for s, q in self.positions.items() if q > 0)
        short_mv = sum(q * self.last_price.get(s, 0.0) for s, q in self.positions.items() if q < 0)
        return AccountInfo(equity=eq, cash=self.cash, buying_power=max(self.cash, 0.0), account_number=self.account_number, status="ACTIVE",
                           shorting_enabled=self.shorting_enabled, last_equity=eq, long_market_value=long_mv, short_market_value=short_mv)

    def verify_account_env(self):
        return True, "sim"

    def get_positions(self) -> dict[str, BrokerPosition]:
        return {s: BrokerPosition(s, q, self.avg_price.get(s, 0.0), q * self.last_price.get(s, 0.0), self.last_price.get(s, 0.0), abs(q))
                for s, q in self.positions.items() if abs(q) > 1e-12}

    def get_open_orders(self) -> list[OrderInfo]:
        return [o.info() for o in self.by_id.values() if o.status in OPEN and not o.held]

    def get_order_by_client_id(self, cid: str) -> OrderInfo | None:
        o = self.orders.get(cid)
        return o.info() if o else None

    def get_order_by_id(self, oid: str) -> OrderInfo | None:
        o = self.by_id.get(oid)
        return o.info() if o else None

    def get_asset(self, symbol: str) -> AssetInfo:
        return AssetInfo(symbol, True, not self.whole_share_only, True, True, True)

    def get_latest_quote(self, symbol: str) -> QuoteInfo | None:
        p = self.last_price.get(symbol)
        if p is None or self.now is None:
            return None
        half = p * self.last_quote_spread.get(symbol, 2.0) / 2e4
        return QuoteInfo(symbol, self.now, p - half, p + half, 100, 100)

    def get_clock(self):
        from bot.execution.broker import ClockInfo
        return ClockInfo(self.now, True, self.now, self.now)

    def get_sessions(self, start, end):
        from bot.data.sessions import SessionCalendar
        return SessionCalendar().sessions_between(start, end)

    # ---------------------------------------------------------------- submit
    def _new(self, symbol, qty, side, cid, tif, order_type, limit_price=None, stop_price=None, order_class="simple", parent=None) -> SimOrder:
        if cid in self.orders:
            from bot.execution.fake_broker import DuplicateClientOrderId
            raise DuplicateClientOrderId(cid)
        q = float(qty)
        if q <= 0:
            raise ValueError("qty must be positive")
        frac = abs(q - round(q)) > 1e-9
        if frac and tif != "day":
            raise ValueError("fractional orders must be DAY")
        if frac and order_class != "simple":
            raise ValueError("fractional orders cannot be bracket/OTO")
        if frac and self.whole_share_only:
            raise ValueError("account is whole-share only")
        o = SimOrder(str(next(self._ids)), cid, symbol, side, q, order_type, tif, limit_price, stop_price, order_class,
                     submitted_at=self.now, parent_id=parent, held=parent is not None, decision_ts=self.now)
        self.orders[cid] = o
        self.by_id[o.id] = o
        self.last_request_id = f"sim-{o.id}"
        self._emit(o, "new")
        return o

    def submit_market_order(self, symbol, qty, side, client_order_id, tif="day") -> OrderInfo:
        return self._new(symbol, qty, side, client_order_id, tif, "market").info()

    def submit_limit_order(self, symbol, qty, side, limit_price, client_order_id, tif="day", extended_hours=False) -> OrderInfo:
        return self._new(symbol, qty, side, client_order_id, tif, "limit", limit_price=round(float(limit_price), 2)).info()

    def submit_stop_order(self, symbol, qty, side, stop_price, client_order_id, tif="gtc") -> OrderInfo:
        return self._new(symbol, qty, side, client_order_id, tif, "stop", stop_price=round(float(stop_price), 2)).info()

    def submit_oto(self, symbol, qty, side, client_order_id, *, stop_price, entry_type="market", limit_price=None, tif="day") -> OrderInfo:
        parent = self._new(symbol, qty, side, client_order_id, tif, entry_type, limit_price=limit_price, order_class="oto")
        leg = self._new(symbol, qty, "sell" if side == "buy" else "buy", client_order_id + "-stop", "gtc", "stop", stop_price=stop_price, parent=parent.id)
        parent.legs.append(leg)
        return parent.info()

    def submit_bracket(self, symbol, qty, side, client_order_id, *, take_profit_price, stop_price, entry_type="market", limit_price=None, tif="day") -> OrderInfo:
        parent = self._new(symbol, qty, side, client_order_id, tif, entry_type, limit_price=limit_price, order_class="bracket")
        exit_side = "sell" if side == "buy" else "buy"
        tp = self._new(symbol, qty, exit_side, client_order_id + "-tp", "gtc", "limit", limit_price=take_profit_price, parent=parent.id)
        sl = self._new(symbol, qty, exit_side, client_order_id + "-stop", "gtc", "stop", stop_price=stop_price, parent=parent.id)
        parent.legs += [tp, sl]
        return parent.info()

    def replace_order(self, order_id: str, *, qty=None, limit_price=None, stop_price=None) -> OrderInfo:
        o = self.by_id[order_id]
        if o.status not in OPEN:
            raise ValueError(f"order {order_id} is {o.status}; cannot replace")
        if qty is not None:
            o.qty = float(qty)
        if limit_price is not None:
            o.limit_price = round(float(limit_price), 2)
        if stop_price is not None:
            o.stop_price = round(float(stop_price), 2)
        self._emit(o, "replaced")
        return o.info()

    def cancel_order(self, order_id: str) -> None:
        o = self.by_id.get(order_id)
        if o and o.status in OPEN:
            o.status = "canceled"
            self._emit(o, "canceled")
            for leg in o.legs:
                if leg.status in OPEN:
                    leg.status = "canceled"
                    self._emit(leg, "canceled")

    def cancel_all_orders(self) -> None:
        for o in list(self.by_id.values()):
            self.cancel_order(o.id)

    def close_all_positions(self) -> None:
        for s, q in list(self.positions.items()):
            if abs(q) > 1e-12:
                o = self._new(s, abs(q), "sell" if q > 0 else "buy", f"close-all-{s}-{next(self._ids)}", "day", "market")
                px = self.last_price.get(s, self.avg_price.get(s, 0.0))
                self._fill(o, abs(q), px, "close_all", 0.0)

    # ----------------------------------------------------------------- stepping
    def set_quote_spread(self, symbol: str, spread_bps: float) -> None:
        self.last_quote_spread[symbol] = spread_bps

    def mark(self, symbol: str, price: float, ts: datetime) -> None:
        self.last_price[symbol] = price
        self.now = ts

    def step(self, bar: BarEvent) -> None:
        """Feed the NEXT 1-minute bar for ``bar.symbol``: pending market/limit orders fill against it, elected stops
        fill at its open, un-elected stops check election on it. Then mark to market at its close."""
        self.now = bar.ts
        kind = self.symbol_kinds.get(bar.symbol, "etf")
        spread = self.last_quote_spread.get(bar.symbol)
        for o in list(self.by_id.values()):
            if o.symbol != bar.symbol or o.status not in OPEN or o.held or o.tif in ("opg", "cls"):
                continue
            if o.decision_ts is not None and bar.ts <= o.decision_ts:
                continue   # no lookahead: only bars strictly after the decision
            remaining = o.qty - o.filled_qty
            if o.order_type == "stop":
                if o.elected:
                    gapped = (bar.open <= o.stop_price) if o.sign < 0 else (bar.open >= o.stop_price)
                    f = self.fills.stop_fill(o.sign, remaining, o.stop_price, bar, gapped=gapped)
                    self._fill(o, f.qty, f.price, f.reason, f.costs_bps)
                elif self.fills.stop_elected(o.sign, o.stop_price, bar):
                    o.elected = True
                continue
            f = self.fills.marketable_limit(o.sign, remaining, bar, symbol_kind=kind, quoted_spread_bps=spread,
                                            limit_price=o.limit_price if o.order_type == "limit" else None, market=(o.order_type == "market"))
            if f is not None and f.qty > 0:
                self._fill(o, f.qty, f.price, f.reason, f.costs_bps)
        self.mark(bar.symbol, bar.close, bar.ts)   # clock = start of the last COMPLETED bar; decisions made now fill on the next bar

    def step_daily(self, bar: BarEvent) -> None:
        """Daily-legacy mode: open market orders fill at this bar's open with V1 costs and V1's cash-affordability rule
        (whole-share floor of what cash allows, even for fractional quantities), exactly like engine.py."""
        self.now = bar.ts
        for o in list(self.by_id.values()):
            if o.symbol != bar.symbol or o.status not in OPEN or o.held or o.order_type != "market":
                continue
            if o.decision_ts is not None and bar.ts <= o.decision_ts:
                continue
            remaining = o.qty - o.filled_qty
            f = self.fills.legacy_daily(o.sign, remaining, bar.open)
            qty = f.qty
            if o.sign > 0 and self.positions.get(o.symbol, 0.0) >= 0:   # entry-side buy: V1 cash cap
                comm = self.fills.p.commission_per_share * qty
                affordable = math.floor((self.cash - comm) / f.price) if f.price > 0 else 0
                qty = min(qty, max(affordable, 0))
                if qty <= 0:
                    o.status = "canceled"
                    self._emit(o, "canceled")
                    continue
            # V1 books slippage inside the fill price and counts it in costs_paid
            self.costs_paid += qty * abs(f.price - bar.open)
            self.traded_notional += qty * f.price
            self._fill(o, qty, f.price, f.reason, 0.0)
        self.mark(bar.symbol, bar.close, bar.ts)

    def session_open(self, symbol: str, official_open: float, ts: datetime) -> None:
        self.now = ts
        for o in list(self.by_id.values()):
            if o.symbol == symbol and o.status in OPEN and not o.held and o.tif == "opg":
                f = self.fills.auction_open(o.sign, o.qty - o.filled_qty, official_open)
                self._fill(o, f.qty, f.price, f.reason, f.costs_bps)
        self.mark(symbol, official_open, ts)

    def session_close(self, symbol: str, official_close: float, ts: datetime) -> None:
        self.now = ts
        for o in list(self.by_id.values()):
            if o.symbol == symbol and o.status in OPEN and not o.held and o.tif == "cls":
                f = self.fills.auction_close(o.sign, o.qty - o.filled_qty, official_close)
                self._fill(o, f.qty, f.price, f.reason, f.costs_bps)
        self.mark(symbol, official_close, ts)

    def end_of_day(self, ts: datetime) -> None:
        """DAY (and unfilled OPG/CLS) orders expire; GTC survive."""
        self.now = ts
        for o in list(self.by_id.values()):
            if o.status in OPEN and o.tif in ("day", "opg", "cls") and not o.held:
                o.status = "expired" if o.filled_qty == 0 else "done_for_day"
                self._emit(o, "expired" if o.filled_qty == 0 else "done_for_day")
                for leg in o.legs:
                    if leg.status in OPEN and leg.held:
                        leg.status = "canceled"
                        self._emit(leg, "canceled")

    # ------------------------------------------------------------------ fills
    def _fill(self, o: SimOrder, qty: float, price: float, reason: str, costs_bps: float) -> None:
        if qty <= 0:
            return
        qty = min(qty, o.qty - o.filled_qty)
        signed = o.sign * qty
        prev = self.positions.get(o.symbol, 0.0)
        new = prev + signed
        if abs(new) > 1e-12 and (prev == 0 or (prev > 0) == (new > 0)) and abs(new) > abs(prev):
            self.avg_price[o.symbol] = (abs(prev) * self.avg_price.get(o.symbol, price) + qty * price) / abs(new)
        if abs(new) <= 1e-12:
            self.positions.pop(o.symbol, None)
            self.avg_price.pop(o.symbol, None)
        else:
            self.positions[o.symbol] = new
        comm = self.fills.p.commission_per_share * qty
        self.cash -= signed * price + comm
        self.costs_paid += abs(qty * price * costs_bps / 1e4) + comm
        self.traded_notional += qty * price
        o.filled_qty += qty
        o.filled_notional += qty * price
        o.filled_at = self.now
        done = o.filled_qty >= o.qty - 1e-9
        o.status = "filled" if done else "partially_filled"
        self.fills_log.append({"ts": self.now, "client_order_id": o.client_order_id, "symbol": o.symbol, "side": o.side, "qty": qty,
                               "price": price, "reason": reason, "costs_bps": costs_bps})
        self._emit(o, "fill" if done else "partial_fill", price=price, qty=qty)
        if done:
            for leg in o.legs:            # OTO/bracket legs go live on the parent fill
                leg.held = False
                leg.status = "new"
            if o.parent_id is not None:  # one bracket leg filled -> cancel the sibling
                parent = self.by_id.get(o.parent_id)
                if parent:
                    for sib in parent.legs:
                        if sib is not o and sib.status in OPEN:
                            sib.status = "canceled"
                            self._emit(sib, "canceled")

    def _emit(self, o: SimOrder, event: str, price: float | None = None, qty: float | None = None) -> None:
        if self.on_trade_update is None:
            return
        ev = TradeUpdateEvent(o.id, o.client_order_id, event, self.now, o.symbol, o.side, o.qty, o.filled_qty, price, o.status,
                              {"leg_qty": qty, "parent_id": o.parent_id, "order_type": o.order_type, "tif": o.tif})
        self.on_trade_update(ev)
