"""One decision step shared by the backtester and the live daemon (spec §2: same decision path).

modules -> TradeIntents (per listened kind, in KIND_ORDER) -> stress hooks (backtest only) -> RiskEngine.admit ->
Allocator.allocate -> OrderManager.reconcile. The function takes its collaborators as arguments and imports nothing
from bot.execution, so the portfolio layer stays below the order path.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any, Callable

from bot.core.events import ScheduleEvent
from bot.core.intents import TradeIntent
from bot.portfolio.allocator import Allocator, PositionView
from bot.strategies.adapter import PositionView as SlicePos

KIND_ORDER = ("pre_open", "session_open", "bar_close_1m", "bar_close_30m", "t1530", "t1550", "t1558", "session_close", "post_close", "evening")
KIND_EVENTS = {"bar_close_1m": ("bar_close", "1m"), "bar_close_30m": ("bar_close", "30m")}


def slice_view(risk, module_id: str, symbol: str, prices: dict[str, float], equity: float) -> SlicePos:
    sl = risk.ledger.slice(module_id, symbol)
    px = prices.get(symbol, sl.avg_price)
    w = (sl.qty * px / equity) if (equity > 0 and px) else None
    return SlicePos(sl.qty, avg_price=sl.avg_price if abs(sl.qty) > 1e-12 else None, weight=w)


def collect_intents(modules, *, kinds: set[str], ts: datetime, session: date, symbols: list[str], features, risk, prices: dict[str, float],
                    equity: float) -> list[TradeIntent]:
    """Deliver one ScheduleEvent per (module, listened kind) in KIND_ORDER; cross-sectional modules get on_event_batch."""
    out: list[TradeIntent] = []
    for mod in modules:
        hit = [k for k in KIND_ORDER if k in kinds and k in mod.listens]
        if not hit:
            continue
        syms = [x for x in mod.symbols if x in symbols]
        if hasattr(mod, "on_event_batch"):
            snaps = {x: features.snapshot(x, ts) for x in syms}
            poss = {x: slice_view(risk, mod.module_id, x, prices, equity) for x in syms}
            for k in hit:
                kind, tf = KIND_EVENTS.get(k, (k, None))
                out.extend(mod.on_event_batch(ScheduleEvent(kind, ts, session, tf), snaps, poss))
            continue
        for sym in syms:
            snap = features.snapshot(sym, ts)
            for k in hit:
                kind, tf = KIND_EVENTS.get(k, (k, None))
                out.extend(mod.on_event(ScheduleEvent(kind, ts, session, tf), snap, slice_view(risk, mod.module_id, sym, prices, equity)))
    return out


def dispatch_intents(intents: list[TradeIntent], *, ts: datetime, session: date, account, risk, allocator: Allocator, oms, prices: dict[str, float],
                     spreads: dict[str, float], stale: dict[str, float], symbol_kinds: dict[str, str], recent_sessions: list[date],
                     market_open: bool = True, corr_matrix=None, shortable: Callable[[str], bool] | None = None) -> list[Any]:
    """Admission -> allocation -> orders. Returns the OrderRecords created (any status)."""
    if not intents:
        return []
    admitted = []
    for it in intents:
        d = risk.admit(it, spread_bps=spreads.get(it.symbol), stale_seconds=stale.get(it.symbol), is_etf=symbol_kinds.get(it.symbol, "etf") == "etf",
                       account=account, recent_sessions=recent_sessions, shortable=shortable(it.symbol) if shortable else True)
        if d.approved:
            mult = risk.budget_multiplier(it.module_id)
            admitted.append(it if mult >= 1.0 else TradeIntent(**{**it.__dict__, "risk_budget_pct": it.risk_budget_pct * mult}))
        else:
            oms.decisions.append({"ts": ts.isoformat(), "module": it.module_id, "symbol": it.symbol, "decision": "not_admitted", "detail": f"{d.code}: {d.detail}"})
            if getattr(oms, "store", None) is not None:
                oms.store.add_decision(oms.decisions[-1])
    if not admitted:
        return []
    views = [PositionView(x.symbol, x.module_id, x.qty, prices.get(x.symbol, x.avg_price)) for x in risk.ledger.slices.values() if abs(x.qty) > 1e-12]
    alloc = allocator.allocate(admitted, views, account.equity, cash=account.cash, corr_matrix=corr_matrix)
    return oms.reconcile(alloc.targets, account=account, prices=prices, session=session, market_open=market_open, spreads=spreads, stale=stale)


class StressHooks:
    """Backtest-only signal stresses (dropped signals, delayed execution), seeded and deterministic."""

    def __init__(self, drop_fraction: float = 0.0, delay_bars: int = 0, seed: int = 0):
        self.drop, self.delay, self.rng = drop_fraction, delay_bars, random.Random(seed)
        self.queue: list[tuple[int, TradeIntent]] = []

    def apply(self, intents: list[TradeIntent], ts: datetime, decisions: list[dict]) -> list[TradeIntent]:
        if self.drop > 0:
            kept = []
            for it in intents:
                if it.direction != 0 and self.rng.random() < self.drop:
                    decisions.append({"ts": ts.isoformat(), "module": it.module_id, "symbol": it.symbol, "decision": "dropped(stress)", "detail": ""})
                else:
                    kept.append(it)
            intents = kept
        if self.delay > 0:
            self.queue.extend((self.delay, it) for it in intents)
            intents = []
        ready = []
        for i in range(len(self.queue) - 1, -1, -1):
            n, it = self.queue[i]
            if n <= 0:
                ready.append(it); self.queue.pop(i)
            else:
                self.queue[i] = (n - 1, it)
        return intents + list(reversed(ready))


def recent_sessions(calendar, session: date) -> list[date]:
    return [x.date for x in calendar.sessions_between(session - timedelta(days=9), session)]
