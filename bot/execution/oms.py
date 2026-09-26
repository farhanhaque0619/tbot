"""OrderManager (Phase 2/4, spec §8): target positions -> orders, state machine on trade updates, protective legs.

Order priority within an event: risk exits, then module exits, then entries. Every order passes
``RiskEngine.check`` (the existing RiskManager gate). Client ids: ``<run>-<module>-<symbol>-<session>-<seq>-<kind>``.
Trade updates are idempotent by (order_id, event, ts). Protective policy: whole-share overnight entries via
market/limit carry an OTO stop leg; auction (OPG/CLS) entries get a standalone GTC stop on fill; fractional
positions get a DAY stop on fill that is re-placed at every pre-open and are flagged ``unprotected_overnight``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from bot.core.events import ScheduleEvent, TradeUpdateEvent
from bot.core.intents import TargetPosition
from bot.core.policy import RiskPolicy
from bot.risk.manager import OrderIntent
from bot.risk.policy_engine import RiskEngine

log = logging.getLogger(__name__)
TICK = 0.01
OPEN_STATES = {"new", "accepted", "partially_filled", "pending_new", "held"}


@dataclass
class OrderRecord:
    client_order_id: str
    module_id: str
    symbol: str
    side: str
    qty: float
    kind: str                       # entry | exit | stop | tp | flatten
    style: str
    session: date
    decision_ts: datetime
    reference_price: float
    broker_id: str | None = None
    status: str = "submitting"
    filled_qty: float = 0.0
    avg_price: float | None = None
    protective_for: str | None = None
    repriced: bool = False
    abandon_at: datetime | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = "day"
    events: list[str] = field(default_factory=list)
    risk: dict[str, Any] | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES


class OrderManager:
    def __init__(self, broker, risk: RiskEngine, policy: RiskPolicy, *, run_id: str, clock: Callable[[], datetime],
                 whole_share_capable: bool = False, on_alert: Callable[[str, str], None] | None = None,
                 software_stops: bool = False, symbol_kind: Callable[[str], str] | None = None, store=None):
        self.broker, self.risk, self.policy, self.run_id, self.clock = broker, risk, policy, run_id, clock
        self.store = store                        # ExecutionStore or None (backtests)
        self.whole = whole_share_capable
        self.alert = on_alert or (lambda title, msg: None)
        self.software_stops = software_stops      # legacy daily mode: stop checked on the close by the engine, no broker leg
        self.symbol_kind = symbol_kind or (lambda s: "etf")
        self.orders: dict[str, OrderRecord] = {}
        self.by_broker_id: dict[str, OrderRecord] = {}
        self.seen_events: set[tuple] = set()
        self._seq: dict[tuple[str, str, date], int] = {}
        self.pending_flatten: set[tuple[str, str]] = set()
        self.trades: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.protective: dict[tuple[str, str], str] = {}       # (module, symbol) -> client id of live protective order
        if store is not None:
            self.seen_events = set(store.seen_events())

    # ------------------------------------------------------------ persistence
    def _persist(self, rec: OrderRecord) -> None:
        if self.store is not None:
            self.store.update_order(rec)

    def restore(self) -> int:
        """Rebuild orders, slices and protective map from the store after a restart. Returns the number of orders."""
        if self.store is None:
            return 0
        n = 0
        for row in self.store.orders():
            rec = OrderRecord(row["client_order_id"], row["module"], row["symbol"], row["side"], float(row["qty"]), row["kind"], row["style"],
                              date.fromisoformat(row["session"]) if row["session"] else self.clock().date(),
                              datetime.fromisoformat(row["decision_ts"]) if row["decision_ts"] else self.clock(), float(row["reference_price"] or 0.0),
                              broker_id=row["broker_id"], status=row["status"], filled_qty=float(row["filled_qty"] or 0), avg_price=row["avg_price"],
                              protective_for=row["protective_for"], repriced=bool(row["repriced"]),
                              abandon_at=datetime.fromisoformat(row["abandon_at"]) if row["abandon_at"] else None, limit_price=row["limit_price"],
                              stop_price=row["stop_price"], tif=row["tif"] or "day", events=list(row["events"] or []), risk=row["risk"])
            self.orders[rec.client_order_id] = rec
            if rec.broker_id:
                self.by_broker_id[rec.broker_id] = rec
            key = (rec.module_id, rec.symbol, rec.session)
            try:
                seq = int(rec.client_order_id.rsplit("-", 2)[1])
                self._seq[key] = max(self._seq.get(key, 0), seq)
            except (ValueError, IndexError):
                pass
            n += 1
        self.store.load_slices(self.risk.ledger)
        for (m, s), p in self.store.protectives().items():
            if p["client_order_id"] in self.orders and self.orders[p["client_order_id"]].is_open:
                self.protective[(m, s)] = p["client_order_id"]
        return n

    # ------------------------------------------------------------------ ids
    def _cid(self, module: str, symbol: str, session: date, kind: str) -> str:
        key = (module, symbol, session)
        self._seq[key] = self._seq.get(key, 0) + 1
        return f"{self.run_id}-{module}-{symbol}-{session.isoformat()}-{self._seq[key]}-{kind}"

    # -------------------------------------------------------------- planning
    def plan(self, targets: list[TargetPosition]) -> list[TargetPosition]:
        """Order priority: exits (kind=exit or target 0), then entries."""
        exits = [t for t in targets if t.kind == "exit" or t.is_flat]
        entries = [t for t in targets if t not in exits]
        return exits + entries

    def reconcile(self, targets: list[TargetPosition], *, account, prices: dict[str, float], session: date, market_open: bool,
                  spreads: dict[str, float] | None = None, stale: dict[str, float] | None = None, bar_current: bool = True) -> list[OrderRecord]:
        """Turn targets into orders relative to the ledger's module slices. Returns records created (any status)."""
        created: list[OrderRecord] = []
        now = self.clock()
        for t in self.plan(targets):
            sl = self.risk.ledger.slice(t.module_id, t.symbol)
            delta = t.target_qty - sl.qty
            if abs(delta) < 1e-9:
                continue
            if any(o.symbol == t.symbol and o.module_id == t.module_id and o.is_open and o.kind in ("entry", "exit") for o in self.orders.values()):
                self._decide(t, None, "skip", "open order for this slice")
                continue
            side = "buy" if delta > 0 else "sell"
            is_exit = (sl.qty != 0 and abs(t.target_qty) < abs(sl.qty)) or t.is_flat
            kind = "exit" if is_exit else "entry"
            qty = abs(delta) if is_exit else abs(delta)
            if is_exit:
                qty = min(abs(delta), abs(sl.qty))
            style = t.exit_style if is_exit else t.entry_style
            if is_exit:
                # a resting protective stop and an exit could both fill: cancel the stop first, then submit the exit
                self._cancel_protective(t.module_id, t.symbol, reason="module exit")
            style, tif, order_type, limit_price = self._translate_style(style, side, qty, prices.get(t.symbol, t.reference_price), spreads, t.symbol, now)
            if style is None:
                self._decide(t, None, "waiting", f"style {t.exit_style if is_exit else t.entry_style} not eligible now")
                continue
            cid = self._cid(t.module_id, t.symbol, session, kind)
            intent = OrderIntent(t.symbol, side, qty, kind, prices.get(t.symbol, t.reference_price), cid, tif, t.intent.tag if t.intent else "")
            state = self._state(t.symbol, prices, spreads, stale, market_open)
            asset = None
            try:
                asset = self.broker.get_asset(t.symbol)
            except Exception:  # noqa: BLE001
                pass
            dec = self.risk.check(intent, state=state, account=account, asset=asset, broker_env=getattr(self.broker, "env", "paper"),
                                  expected_env=getattr(self.broker, "env", "paper"), account_is_paper_shaped=None,
                                  open_orders=[o for o in self._open_broker_orders()], known_client_ids=list(self.orders),
                                  position_qty=sl.qty, n_positions=self.risk.ledger.open_positions(), tif=tif, bar_current=bar_current)
            rec = OrderRecord(cid, t.module_id, t.symbol, side, qty, kind, style, session, now, intent.reference_price, tif=tif,
                              limit_price=limit_price, stop_price=None, risk=dec.to_dict())
            if not dec.approved:
                rec.status = "risk_rejected"
                self.orders[cid] = rec
                self._persist(rec)
                self._decide(t, rec, "blocked", f"{dec.code}: {dec.detail}")
                created.append(rec)
                continue
            self.orders[cid] = rec
            self.risk.ledger.inflight[cid] = (t.module_id, t.symbol, (1 if side == "buy" else -1) * qty * intent.reference_price)
            protective = t.protective_stop_price if (kind == "entry" and t.overnight_ok and not self.software_stops) else None
            if self.store is not None:
                self.store.begin_submit(rec)          # committed BEFORE the broker call (crash-safe: cid is deterministic)
            try:
                info = self._submit(rec, order_type, limit_price, protective, whole=self._is_whole(qty))
            except Exception as e:  # noqa: BLE001
                rec.status, rec.events = "submit_failed", rec.events + [f"submit failed: {type(e).__name__}: {e}"]
                self.risk.ledger.inflight.pop(cid, None)
                self._persist(rec)
                self._decide(t, rec, "error", str(e))
                created.append(rec)
                continue
            rec.broker_id, rec.status = info.id, info.status
            if protective is not None and self._is_whole(qty) and order_type in ("market", "limit"):
                rec.events.append(f"OTO stop leg at {protective:.2f}")
                self.protective[(t.module_id, t.symbol)] = cid + "-stop"
            rec.abandon_at = self._abandon_at(style, now, session)
            rec.stop_price = protective if protective is not None else (t.protective_stop_price if kind == "entry" else None)
            self.by_broker_id[info.id] = rec
            self._persist(rec)
            self._decide(t, rec, "submit", f"{side} {qty:g} {style} {tif}")
            created.append(rec)
        return created

    # ----------------------------------------------------------- translation
    def _is_whole(self, qty: float) -> bool:
        return abs(qty - round(qty)) < 1e-9 and qty >= 1

    def _translate_style(self, style: str, side: str, qty: float, price: float, spreads, symbol: str, now: datetime):
        """Map an entry/exit style to (style, tif, order_type, limit_price) given account/time constraints. None = wait."""
        whole = self._is_whole(qty)
        t = now.timetz().replace(tzinfo=None)
        if style in ("opg",) and not whole:
            style = "market"                   # fractional cannot use auction orders; first minute market order instead
        if style in ("cls",) and not whole:
            style = "market_1558"
        if style == "opg":
            return "opg", "opg", "market", None
        if style == "cls":
            if time(15, 50) <= t < time(16, 0):
                return None, None, None, None  # past the CLS cutoff; wait for the evening window / next day handled by caller
            return "cls", "cls", "market", None
        if style in ("market", "market_1555", "market_1558"):
            return style, "day", "market", None
        if style == "marketable_limit":
            spread_bps = (spreads or {}).get(symbol)
            tick = TICK
            half = price * (spread_bps or 2.0) / 2e4
            ask, bid = price + half, price - half
            lp = round(ask + tick, 2) if side == "buy" else round(bid - tick, 2)
            return "marketable_limit", "day", "limit", lp
        if style == "limit_at_prev_close_cancel_0945":
            return style, "day", "limit", round(price, 2)
        return style, "day", "market", None

    def _abandon_at(self, style: str, now: datetime, session: date) -> datetime | None:
        if style == "marketable_limit":
            return datetime.combine(session, time(15, 33), now.tzinfo) if now.time() >= time(15, 0) else now + timedelta(minutes=3)
        if style == "limit_at_prev_close_cancel_0945":
            return datetime.combine(session, time(9, 45), now.tzinfo)
        return None

    def _submit(self, rec: OrderRecord, order_type: str, limit_price: float | None, protective: float | None, *, whole: bool):
        b = self.broker
        if protective is not None and whole and order_type in ("market", "limit") and hasattr(b, "submit_oto"):
            return b.submit_oto(rec.symbol, rec.qty, rec.side, rec.client_order_id, stop_price=protective, entry_type=order_type,
                                limit_price=limit_price, tif=rec.tif)
        if order_type == "limit":
            return b.submit_limit_order(rec.symbol, rec.qty, rec.side, limit_price, rec.client_order_id, rec.tif)
        return b.submit_market_order(rec.symbol, rec.qty, rec.side, rec.client_order_id, rec.tif)

    def _open_broker_orders(self):
        try:
            return self.broker.get_open_orders()
        except Exception:  # noqa: BLE001
            return []

    def _state(self, symbol: str, prices, spreads, stale, market_open: bool):
        from bot.execution.market_state import MarketState
        import dataclasses
        fields = [f.name for f in dataclasses.fields(MarketState)]
        px = prices.get(symbol, float("nan"))
        sp = (spreads or {}).get(symbol, float("nan"))
        vals = {"timestamp": self.clock().isoformat(), "symbol": symbol, "market_open": market_open, "last_price": px, "mid": px,
                "bid": px, "ask": px, "spread_bps": sp if sp is not None else float("nan"),
                "stale_data_seconds": (stale or {}).get(symbol, 0.0), "open_orders": 0, "last_bar_date": self.clock().date().isoformat(), "n_bars": 1,
                "position_qty": self.risk.ledger.symbol_qty(symbol), "gross_exposure": self.risk.ledger.gross(prices), "net_exposure": self.risk.ledger.net(prices),
                "daily_pnl": 0.0, "drawdown": 0.0}
        return MarketState(**{f: vals.get(f, float("nan")) for f in fields})

    def _decide(self, t: TargetPosition, rec: OrderRecord | None, decision: str, detail: str) -> None:
        d = {"ts": self.clock().isoformat(), "module": t.module_id, "symbol": t.symbol, "target_qty": t.target_qty,
             "decision": decision, "detail": detail, "client_order_id": rec.client_order_id if rec else None, "risk": rec.risk if rec else None}
        self.decisions.append(d)
        if self.store is not None:
            self.store.add_decision(d)

    # -------------------------------------------------------- trade updates
    def on_trade_update(self, ev: TradeUpdateEvent, *, session: date | None = None) -> None:
        if ev.key in self.seen_events:
            return
        self.seen_events.add(ev.key)
        if self.store is not None and not self.store.record_fill(ev):
            return                                   # already applied before a restart
        self._on_trade_update(ev, session=session)
        if self.store is not None:
            rec = self.orders.get(ev.client_order_id) or self.by_broker_id.get(ev.order_id)
            if rec is not None:
                self.store.update_order(rec)
            self.store.save_slices(self.risk.ledger)

    def _on_trade_update(self, ev: TradeUpdateEvent, *, session: date | None = None) -> None:
        rec = self.orders.get(ev.client_order_id) or self.by_broker_id.get(ev.order_id)
        if rec is None:
            if ev.client_order_id.endswith("-stop") or ev.client_order_id.endswith("-tp"):
                parent = self.orders.get(ev.client_order_id.rsplit("-", 1)[0])
                if parent is not None:
                    rec = OrderRecord(ev.client_order_id, parent.module_id, parent.symbol, ev.side, ev.qty, "stop" if ev.client_order_id.endswith("-stop") else "tp",
                                      "protective", parent.session, parent.decision_ts, parent.reference_price, broker_id=ev.order_id, status=ev.status,
                                      protective_for=parent.client_order_id, tif="gtc")
                    self.orders[rec.client_order_id] = rec
                    self.by_broker_id[ev.order_id] = rec
            if rec is None:
                log.warning("trade update for unknown order %s/%s (%s)", ev.order_id, ev.client_order_id, ev.event)
                return
        rec.broker_id = rec.broker_id or ev.order_id
        rec.events.append(f"{ev.ts.isoformat()} {ev.event}")
        prev_filled = rec.filled_qty
        rec.status = ev.status
        if ev.event in ("fill", "partial_fill"):
            inc = ev.filled_qty - prev_filled
            if inc > 1e-12:
                px = ev.price if ev.price is not None else rec.reference_price
                rec.avg_price = ((rec.avg_price or 0.0) * prev_filled + px * inc) / ev.filled_qty
                rec.filled_qty = ev.filled_qty
                signed = inc if rec.side == "buy" else -inc
                sess = session or rec.session
                before = self.risk.ledger.slice(rec.module_id, rec.symbol).qty
                self.risk.ledger.apply_fill(rec.module_id, rec.symbol, signed, px, sess)
                after = self.risk.ledger.slice(rec.module_id, rec.symbol).qty
                if rec.kind in ("exit", "stop", "tp", "flatten") or (before != 0 and abs(after) < abs(before)):
                    self._book_trade(rec, inc, px, sess)
            if ev.event == "fill":
                self.risk.ledger.inflight.pop(rec.client_order_id, None)
                if rec.kind == "entry":
                    self._after_entry_fill(rec)
                if rec.kind in ("stop", "tp"):
                    self.protective.pop((rec.module_id, rec.symbol), None)
        elif ev.event in ("canceled", "expired", "rejected", "done_for_day"):
            self.risk.ledger.inflight.pop(rec.client_order_id, None)
            if rec.kind == "stop" and rec.protective_for:
                key = (rec.module_id, rec.symbol)
                sl = self.risk.ledger.slice(*key)
                if abs(sl.qty) > 1e-12 and ev.event in ("rejected", "canceled") and self.protective.get(key) == rec.client_order_id:
                    sl.unprotected = True
                    self.protective.pop(key, None)
                    self.alert("protective order lost", f"{rec.symbol} {rec.module_id}: {ev.event}; position UNPROTECTED")
                    if self.policy.require_broker_protection_overnight and not self.software_stops:
                        self.pending_flatten.add(key)

    def _after_entry_fill(self, rec: OrderRecord) -> None:
        """Place protection when the entry did not carry an OTO leg (auction entries, fractional positions)."""
        if self.software_stops or rec.stop_price is None:
            return
        key = (rec.module_id, rec.symbol)
        if key in self.protective and self.protective[key] == rec.client_order_id + "-stop":
            return  # OTO leg exists
        sl = self.risk.ledger.slice(*key)
        if abs(sl.qty) < 1e-12:
            return
        self.place_protective(rec.module_id, rec.symbol, rec.stop_price)

    def place_protective(self, module_id: str, symbol: str, stop_price: float) -> OrderRecord | None:
        sl = self.risk.ledger.slice(module_id, symbol)
        if abs(sl.qty) < 1e-12:
            return None
        whole = self._is_whole(abs(sl.qty))
        tif = "gtc" if whole else "day"
        now = self.clock()
        cid = self._cid(module_id, symbol, now.date(), "stop")
        side = "sell" if sl.qty > 0 else "buy"
        rec = OrderRecord(cid, module_id, symbol, side, abs(sl.qty), "stop", "protective", now.date(), now, stop_price, tif=tif, stop_price=stop_price,
                          protective_for=self.protective.get((module_id, symbol)))
        self.orders[cid] = rec
        try:
            info = self.broker.submit_stop_order(symbol, abs(sl.qty), side, round(stop_price, 2), cid, tif)
        except Exception as e:  # noqa: BLE001
            rec.status = "submit_failed"
            rec.events.append(f"protective rejected: {type(e).__name__}: {e}")
            sl.unprotected = True
            self.alert("protective order rejected", f"{symbol} {module_id}: {e}")
            if self.policy.require_broker_protection_overnight:
                self.pending_flatten.add((module_id, symbol))
            return rec
        rec.broker_id, rec.status = info.id, info.status
        self.by_broker_id[info.id] = rec
        old = self.protective.get((module_id, symbol))
        self.protective[(module_id, symbol)] = cid
        sl.protective_order_id, sl.unprotected = cid, False
        sl.unprotected_overnight = not whole
        self._persist(rec)
        if self.store is not None:
            self.store.set_protective(module_id, symbol, cid, stop_price, tif)
        if old and old in self.orders and self.orders[old].is_open and self.orders[old].broker_id:
            try:
                self.broker.cancel_order(self.orders[old].broker_id)
            except Exception as e:  # noqa: BLE001
                log.warning("could not cancel superseded protective %s: %s", old, e)
        return rec

    # ---------------------------------------------------------- scheduling
    def on_schedule(self, ev: ScheduleEvent, *, prices: dict[str, float]) -> None:
        now = ev.ts
        if ev.kind == "pre_open":
            for (m, s), sl in list(self.risk.ledger.slices.items()):
                if abs(sl.qty) > 1e-12 and sl.unprotected_overnight and not self.software_stops:
                    rec = self.orders.get(sl.protective_order_id or "")
                    if rec is not None and rec.stop_price is not None:
                        self.place_protective(m, s, rec.stop_price)
        for rec in list(self.orders.values()):
            if rec.is_open and rec.abandon_at is not None and now >= rec.abandon_at and rec.broker_id:
                try:
                    self.broker.cancel_order(rec.broker_id)
                    rec.events.append("abandoned at cutoff")
                except Exception as e:  # noqa: BLE001
                    log.warning("abandon cancel failed for %s: %s", rec.client_order_id, e)
        if self.pending_flatten and ev.kind in ("session_open", "bar_close", "t1558"):
            for key in list(self.pending_flatten):
                self.flatten_slice(*key, reason="unprotected position and policy requires protection")
                self.pending_flatten.discard(key)

    def tick(self, *, prices: dict[str, float], spreads: dict[str, float] | None = None) -> None:
        """Per-bar housekeeping: re-price stale marketable limits once, abandon orders past their cutoff, flatten pending."""
        self.reprice_stale_limits(prices=prices, spreads=spreads)
        now = self.clock()
        for rec in list(self.orders.values()):
            if rec.is_open and rec.abandon_at is not None and now >= rec.abandon_at and rec.broker_id:
                try:
                    self.broker.cancel_order(rec.broker_id)
                    rec.events.append("abandoned at cutoff")
                    rec.abandon_at = None
                except Exception as e:  # noqa: BLE001
                    log.warning("abandon cancel failed for %s: %s", rec.client_order_id, e)

    def reprice_stale_limits(self, *, prices: dict[str, float], spreads: dict[str, float] | None = None) -> None:
        """M2 entry rule: unfilled marketable limit after one bar -> cancel and re-price once at ask + 2 ticks."""
        for rec in list(self.orders.values()):
            if rec.is_open and rec.style == "marketable_limit" and rec.kind == "entry" and not rec.repriced and rec.broker_id:
                if self.clock() <= rec.decision_ts + timedelta(seconds=20):
                    continue
                price = prices.get(rec.symbol, rec.reference_price)
                half = price * ((spreads or {}).get(rec.symbol, 2.0)) / 2e4
                new_lp = round(price + half + 2 * TICK, 2) if rec.side == "buy" else round(price - half - 2 * TICK, 2)
                try:
                    self.broker.replace_order(rec.broker_id, limit_price=new_lp)
                    rec.limit_price, rec.repriced = new_lp, True
                    rec.events.append(f"repriced to {new_lp}")
                except Exception as e:  # noqa: BLE001
                    rec.events.append(f"reprice failed: {type(e).__name__}")

    def _cancel_protective(self, module_id: str, symbol: str, *, reason: str) -> bool:
        """Cancel the live protective order of a slice (before an exit/flatten so the two cannot both fill)."""
        key = (module_id, symbol)
        cid = self.protective.get(key)
        rec = self.orders.get(cid or "")
        if rec is None or not rec.is_open or not rec.broker_id:
            self.protective.pop(key, None)
            return False
        self.protective.pop(key, None)          # popped first: the resulting 'canceled' update must not flag the slice unprotected
        sl = self.risk.ledger.slice(module_id, symbol)
        sl.protective_order_id = None
        try:
            self.broker.cancel_order(rec.broker_id)
            rec.events.append(f"canceled: {reason}")
            return True
        except Exception as e:  # noqa: BLE001
            rec.events.append(f"cancel failed ({reason}): {type(e).__name__}: {e}")
            self.protective[key] = cid
            sl.protective_order_id = cid
            self.alert("protective cancel failed", f"{symbol} {module_id}: {e}")
            return False

    def flatten_slice(self, module_id: str, symbol: str, *, reason: str) -> OrderRecord | None:
        sl = self.risk.ledger.slice(module_id, symbol)
        if abs(sl.qty) < 1e-12:
            return None
        self._cancel_protective(module_id, symbol, reason=f"flatten: {reason}")
        now = self.clock()
        cid = self._cid(module_id, symbol, now.date(), "flatten")
        side = "sell" if sl.qty > 0 else "buy"
        rec = OrderRecord(cid, module_id, symbol, side, abs(sl.qty), "flatten", "market", now.date(), now, sl.avg_price, tif="day")
        self.orders[cid] = rec
        info = self.broker.submit_market_order(symbol, abs(sl.qty), side, cid, "day")
        rec.broker_id, rec.status = info.id, info.status
        self.by_broker_id[info.id] = rec
        rec.events.append(f"flatten: {reason}")
        self.alert("flatten", f"{symbol} {module_id}: {reason}")
        return rec

    def flatten_all(self, *, reason: str) -> list[OrderRecord]:
        out = []
        for (m, s), sl in list(self.risk.ledger.slices.items()):
            if abs(sl.qty) > 1e-12:
                r = self.flatten_slice(m, s, reason=reason)
                if r:
                    out.append(r)
        return out

    def cancel_non_protective(self) -> int:
        n = 0
        for rec in list(self.orders.values()):
            if rec.is_open and rec.kind in ("entry",) and rec.broker_id:
                try:
                    self.broker.cancel_order(rec.broker_id)
                    n += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("cancel failed %s: %s", rec.client_order_id, e)
        return n

    # ---------------------------------------------------------------- trades
    def _book_trade(self, rec: OrderRecord, qty: float, px: float, session: date) -> None:
        # the ledger already applied the fill; recover the entry price from the record trail
        entry = self._entry_price_for(rec)
        side = 1 if rec.side == "sell" else -1
        pnl = side * (px - entry) * qty if entry is not None else 0.0
        t = {"module": rec.module_id, "symbol": rec.symbol, "qty": qty, "entry_price": entry, "exit_price": px, "pnl": pnl,
             "exit_reason": rec.kind, "exit_ts": self.clock(), "session": session}
        self.trades.append(t)
        throttled = self.risk.record_trade_pnl(rec.module_id, pnl)
        if self.store is not None:
            self.store.add_trade(t)
            if throttled:
                self.store.set_throttle(rec.module_id, self.risk.budget_multiplier(rec.module_id), "last 60 trades net negative; risk budget halved")

    def _entry_price_for(self, rec: OrderRecord) -> float | None:
        entries = [o for o in self.orders.values() if o.module_id == rec.module_id and o.symbol == rec.symbol and o.kind == "entry" and o.filled_qty > 0]
        if not entries:
            return None
        tot = sum(o.filled_qty for o in entries[-3:])
        return sum((o.avg_price or 0.0) * o.filled_qty for o in entries[-3:]) / tot if tot else None
