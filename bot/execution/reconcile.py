"""Reconciliation: the broker is the authority (spec §10).

Compares broker positions with the ledger's module slices, broker open orders with the OrderManager's records, and
orders since the watermark with the fills table. Unknown positions are adopted as module ``orphan`` (with a protective
stop when the policy requires protection) or flattened when ``policy.orphan_policy == "flatten"``; unknown non-protective
open orders are cancelled; unknown protective (stop) orders are adopted. Every discrepancy becomes a ``reconciliation``
decision record and an alert; two consecutive reconciliations with unresolved discrepancies halt entries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from bot.core.events import TradeUpdateEvent
from bot.execution.oms import OrderManager, OrderRecord

log = logging.getLogger(__name__)
ORPHAN = "orphan"


@dataclass
class ReconcileReport:
    ts: datetime
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    adopted_positions: list[str] = field(default_factory=list)
    flattened: list[str] = field(default_factory=list)
    canceled_orders: list[str] = field(default_factory=list)
    adopted_orders: list[str] = field(default_factory=list)
    replayed_fills: int = 0
    halted_entries: bool = False

    @property
    def clean(self) -> bool:
        return not self.discrepancies


def reconcile(*, broker, oms: OrderManager, policy, prices: dict[str, float], session: date, now: datetime, store=None,
              consecutive_key: str = "reconcile_unresolved_streak", alert: Callable[[str, str], None] | None = None,
              orphan_stop_pct: float = 0.05) -> ReconcileReport:
    alert = alert or (lambda t, m: None)
    rep = ReconcileReport(now)
    ledger = oms.risk.ledger

    def disc(kind: str, **kw):
        d = {"ts": now.isoformat(), "kind": kind, "decision": "reconciliation", **kw}
        rep.discrepancies.append(d)
        oms.decisions.append({**d, "module": kw.get("module", ORPHAN), "symbol": kw.get("symbol", ""), "detail": kw.get("detail", kind)})
        if store is not None:
            store.add_decision(d, kind="reconciliation")
        log.warning("reconciliation: %s %s", kind, {k: v for k, v in kw.items() if k != "detail"})

    # ---- 1. orders since the watermark vs fills (FIRST: a fill we missed must not look like an orphan position)
    wm = store.watermark("orders_since") if store is not None else None
    after = datetime.fromisoformat(wm) if wm else None
    if after is not None and hasattr(broker, "get_orders_since"):
        for o in broker.get_orders_since(after):
            rec = oms.orders.get(o.client_order_id) or oms.by_broker_id.get(o.id)
            if rec is None or o.filled_qty <= rec.filled_qty + 1e-12:
                continue
            ev = TradeUpdateEvent(o.id, o.client_order_id, "fill" if o.status == "filled" else "partial_fill", o.filled_at or o.updated_at or now, o.symbol, o.side, o.qty,
                                  o.filled_qty, o.filled_avg_price, o.status, raw={"replayed": True})
            oms.on_trade_update(ev, session=session)
            rep.replayed_fills += 1
            disc("fill_replayed_from_orders", symbol=o.symbol, client_order_id=o.client_order_id, detail=f"filled {o.filled_qty:g} @ {o.filled_avg_price}")
    # ---- 2. positions: broker vs ledger (per symbol totals)
    broker_pos = broker.get_positions()
    ledger_qty: dict[str, float] = {}
    for (_, s), sl in ledger.slices.items():
        ledger_qty[s] = ledger_qty.get(s, 0.0) + sl.qty
    for sym, bp in broker_pos.items():
        diff = bp.qty - ledger_qty.get(sym, 0.0)
        if abs(diff) < 1e-9:
            continue
        disc("position_mismatch", symbol=sym, broker_qty=bp.qty, ledger_qty=ledger_qty.get(sym, 0.0), detail=f"broker {bp.qty:g} vs ledger {ledger_qty.get(sym, 0.0):g}")
        if policy.orphan_policy == "flatten":
            cid = oms._cid(ORPHAN, sym, session, "flatten")
            rec = OrderRecord(cid, ORPHAN, sym, "sell" if diff > 0 else "buy", abs(diff), "flatten", "market", session, now, prices.get(sym, bp.avg_entry_price), tif="day")
            oms.orders[cid] = rec
            try:
                info = broker.submit_market_order(sym, abs(diff), rec.side, cid, "day")
                rec.broker_id, rec.status = info.id, info.status
                oms.by_broker_id[info.id] = rec
                rep.flattened.append(sym)
                alert("reconciliation: flattening orphan", f"{sym} {diff:+g} not owned by any module")
            except Exception as e:  # noqa: BLE001
                rec.status = "submit_failed"
                disc("orphan_flatten_failed", symbol=sym, detail=str(e))
        else:
            ledger.apply_fill(ORPHAN, sym, diff, prices.get(sym, bp.avg_entry_price) or bp.avg_entry_price, session)
            rep.adopted_positions.append(sym)
            alert("reconciliation: adopted orphan position", f"{sym} {diff:+g} adopted as module '{ORPHAN}'")
            if policy.require_broker_protection_overnight and not oms.software_stops:
                px = prices.get(sym, bp.avg_entry_price) or bp.avg_entry_price
                stop = px * (1 - orphan_stop_pct) if diff > 0 else px * (1 + orphan_stop_pct)
                oms.place_protective(ORPHAN, sym, stop)
    for sym, q in ledger_qty.items():
        if abs(q) > 1e-9 and sym not in broker_pos:
            disc("position_missing_at_broker", symbol=sym, ledger_qty=q, detail=f"ledger {q:g} but broker flat: ledger slices cleared")
            for (m, s), sl in list(ledger.slices.items()):
                if s == sym and abs(sl.qty) > 1e-12:
                    ledger.apply_fill(m, s, -sl.qty, prices.get(sym, sl.avg_price), session)
    # ---- 3. open orders: broker vs OMS
    known_broker_ids = {r.broker_id for r in oms.orders.values() if r.broker_id}
    known_cids = set(oms.orders)
    for o in broker.get_open_orders():
        if o.id in known_broker_ids or o.client_order_id in known_cids:
            rec = oms.orders.get(o.client_order_id) or oms.by_broker_id.get(o.id)
            if rec is not None and rec.broker_id is None:
                rec.broker_id, rec.status = o.id, o.status          # crash between our commit and the broker's answer
                oms.by_broker_id[o.id] = rec
                disc("submitting_order_found_at_broker", symbol=o.symbol, client_order_id=o.client_order_id, detail="resolved: broker id attached")
            continue
        if o.order_type in ("stop", "stop_limit"):
            module = ORPHAN
            for (m, s), sl in ledger.slices.items():
                if s == o.symbol and abs(sl.qty) > 1e-12:
                    module = m
            rec = OrderRecord(o.client_order_id, module, o.symbol, o.side, o.qty, "stop", "protective", session, now, o.stop_price or 0.0, broker_id=o.id,
                              status=o.status, tif=o.time_in_force, stop_price=o.stop_price)
            oms.orders[o.client_order_id] = rec
            oms.by_broker_id[o.id] = rec
            oms.protective[(module, o.symbol)] = o.client_order_id
            ledger.slice(module, o.symbol).protective_order_id = o.client_order_id
            rep.adopted_orders.append(o.client_order_id)
            disc("unknown_protective_order_adopted", symbol=o.symbol, client_order_id=o.client_order_id, module=module, detail="adopted as protective")
        else:
            try:
                broker.cancel_order(o.id)
                rep.canceled_orders.append(o.client_order_id)
                disc("unknown_order_canceled", symbol=o.symbol, client_order_id=o.client_order_id, detail=f"{o.side} {o.qty:g} {o.order_type} canceled")
            except Exception as e:  # noqa: BLE001
                disc("unknown_order_cancel_failed", symbol=o.symbol, client_order_id=o.client_order_id, detail=str(e))
    # ---- 3b. rows committed as 'submitting' before a broker call that never completed (crash / SIGKILL mid-submission)
    for rec in list(oms.orders.values()):
        if rec.status != "submitting" or rec.broker_id:
            continue
        found = None
        try:
            found = broker.get_order_by_client_id(rec.client_order_id)
        except Exception as e:  # noqa: BLE001
            log.warning("lookup of %s failed: %s", rec.client_order_id, e)
        if found is not None:
            rec.broker_id, rec.status = found.id, found.status
            oms.by_broker_id[found.id] = rec
            disc("submitting_order_found_at_broker", symbol=rec.symbol, client_order_id=rec.client_order_id, detail="resolved: broker id attached")
        else:
            rec.status = "submit_failed"
            rec.events.append("lost: never reached the broker (crash mid-submission)")
            ledger.inflight.pop(rec.client_order_id, None)
            disc("submitting_order_lost", symbol=rec.symbol, client_order_id=rec.client_order_id, detail="marked submit_failed; slice free again")
        if store is not None:
            store.update_order(rec)
    # ---- 4. streak of unresolved discrepancies -> halt entries
    unresolved = [d for d in rep.discrepancies if d["kind"] in ("position_mismatch", "orphan_flatten_failed", "unknown_order_cancel_failed", "position_missing_at_broker")]
    streak = int((store.meta(consecutive_key) if store is not None else None) or oms.__dict__.get("_reconcile_streak", 0) or 0)
    streak = streak + 1 if unresolved else 0
    oms.__dict__["_reconcile_streak"] = streak
    if store is not None:
        store.set_watermark("orders_since", now)
        store.set_meta(consecutive_key, str(streak))
        store.save_slices(ledger)
    if streak >= 2:
        oms.risk.halt_entries, oms.risk.halt_reason = True, f"{streak} consecutive reconciliations with unresolved discrepancies"
        rep.halted_entries = True
        alert("reconciliation: entries halted", oms.risk.halt_reason)
    if rep.discrepancies:
        alert("reconciliation discrepancies", "; ".join(f"{d['kind']} {d.get('symbol', '')}" for d in rep.discrepancies))
    return rep
