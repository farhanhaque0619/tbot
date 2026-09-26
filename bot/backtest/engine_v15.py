"""V1.5 backtester (Phase 2, spec §4): minute-bar, multi-module, auction-aware; same decision path as live.

Two entry points:
- ``run_daily_legacy(strategies, daily_bars, ...)``: V1 strategies through the LegacyStrategyAdapter on daily bars with
  ``fills=daily_legacy``. Reproduces ``bot/backtest/engine.py`` (fill at next open with V1 costs, stop checked on the
  close, kill switch, end-of-run liquidation) so the frozen Phase 0 numbers are the oracle.
- ``run_minute(modules, bars_1m, calendar, ...)``: shared session clock over all symbols, 30-minute aggregation,
  ScheduleEvents (pre_open, session_open, bar_close, t1530, t1550, t1558, session_close), FeatureEngine snapshots,
  RiskEngine admission, Allocator, OrderManager, SimBroker fills. Order priority: risk exits, module exits, entries.
No lookahead: a fill may only use bars strictly after the decision bar (SimBroker enforces via ``decision_ts``).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

import pandas as pd

from bot.backtest.engine import BacktestResult, Backtester, Trade
from bot.backtest.fills import FillEngine, FillParams
from bot.backtest.metrics import compute_metrics
from bot.backtest.simbroker import SimBroker
from bot.core.events import BarEvent, QuoteEvent, ScheduleEvent, TradeUpdateEvent
from bot.core.intents import TradeIntent
from bot.core.policy import RiskPolicy, legacy_policy
from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from bot.execution.oms import OrderManager
from bot.features.engine import FeatureEngine
from bot.portfolio.allocator import Allocator, PositionView
from bot.risk.policy_engine import RiskEngine
from bot.strategies.adapter import LegacyStrategyAdapter
from bot.strategies.adapter import PositionView as SlicePos
from bot.strategies.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class BacktestResultV15:
    equity: pd.Series
    trades: list[Trade]
    metrics: dict[str, Any]
    module_equity: dict[str, pd.Series] = field(default_factory=dict)
    module_trades: dict[str, list[Trade]] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    costs_paid: float = 0.0
    orders: int = 0
    exposure: pd.Series | None = None
    attribution: dict[str, dict[str, float]] = field(default_factory=dict)   # module -> {overnight, intraday}
    benchmark_equity: pd.Series | None = None
    benchmark_metrics: dict[str, Any] | None = None
    killed: bool = False
    notes: list[str] = field(default_factory=list)
    strategy: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)

    def as_v1(self) -> BacktestResult:
        return BacktestResult(self.strategy, self.params, self.symbols, self.equity, self.equity * 0, self.trades, self.metrics,
                              benchmark_equity=self.benchmark_equity, benchmark_metrics=self.benchmark_metrics, killed=self.killed,
                              orders=self.orders, costs_paid=self.costs_paid, notes=self.notes)


# =============================================================================================== daily legacy
def run_daily_legacy(strategy_factory: Callable[[], Strategy], data: dict[str, pd.DataFrame], *, initial_cash: float = 100_000.0,
                     fills: FillParams | None = None, policy: RiskPolicy | None = None, trade_start=None, trade_end=None,
                     benchmark: bool = True, run_id: str = "bt") -> BacktestResultV15:
    policy = policy or legacy_policy()
    fills = fills or FillParams()
    data = {s.upper(): df for s, df in data.items()}
    symbols = list(data)
    dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    t_start = _loc(trade_start, dates[0]) if trade_start is not None else dates[0]
    t_end = _loc(trade_end, dates[0]) if trade_end is not None else dates[-1]

    updates: list[TradeUpdateEvent] = []
    broker = SimBroker(initial_cash, fills=FillEngine(fills), on_trade_update=updates.append)
    risk = RiskEngine(policy, allow_fractional=True, throttle_enabled=False)   # V1 had no throttle
    features = FeatureEngine(benchmark="__none__")
    allocator = Allocator(policy, whole_share_capable=False)
    oms = OrderManager(broker, risk, policy, run_id=run_id, clock=lambda: broker.now, software_stops=True)
    adapters = {s: LegacyStrategyAdapter(strategy_factory(), policy, symbol=s) for s in symbols}
    arrays = {s: {c: df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "volume")} for s, df in data.items()}
    pos_of = {s: {ts: i for i, ts in enumerate(df.index)} for s, df in data.items()}
    lots: dict[tuple[str, str], dict[str, Any]] = {}
    trades: list[Trade] = []
    eq_hist, ts_hist, gross_hist = [], [], []
    last_close: dict[str, float] = {}
    software_stop: dict[tuple[str, str], float] = {}
    killed_liq = False
    strat_name = adapters[symbols[0]].module_id
    params = dict(adapters[symbols[0]].strategy.params)

    def drain_updates(ts):
        while updates:
            ev = updates.pop(0)
            oms.on_trade_update(ev, session=ts.date())
            rec = oms.orders.get(ev.client_order_id)
            if rec is None or ev.event != "fill":
                continue
            key = (rec.module_id, rec.symbol)
            if rec.kind == "entry":
                lots[key] = {"entry_ts": ts, "entry_price": ev.price, "qty": rec.qty, "bars": 0, "side": 1 if rec.side == "buy" else -1, "reason": rec.risk and "" or "",
                             "entry_reason": rec_tag(rec)}
                if rec.stop_price is not None:
                    software_stop[key] = rec.stop_price
            elif rec.kind in ("exit", "flatten"):
                lot = lots.pop(key, None)
                software_stop.pop(key, None)
                if lot:
                    pnl = lot["side"] * (ev.price - lot["entry_price"]) * lot["qty"]
                    trades.append(Trade(rec.symbol, lot["side"], lot["qty"], lot["entry_ts"], lot["entry_price"], ts, ev.price, pnl,
                                        pnl / (lot["entry_price"] * lot["qty"]), lot["bars"], lot["entry_reason"], rec_tag(rec)))
                    if rec_tag(rec).startswith("stop") or rec_tag(rec) == "kill switch":
                        adapters[rec.symbol].on_position_closed(rec_tag(rec))

    def rec_tag(rec) -> str:
        return rec.events[0][2:] if rec.events and rec.events[0].startswith("t:") else rec.kind

    for ts in dates:
        today = {s: pos_of[s][ts] for s in symbols if ts in pos_of[s]}
        trading = t_start <= ts <= t_end
        # 1. fills at today's open
        for s, i in today.items():
            a = arrays[s]
            broker.step_daily(BarEvent(s, ts, a["open"][i], a["high"][i], a["low"][i], a["close"][i], a["volume"][i], "1d", ts.date(), True))
            last_close[s] = a["close"][i]
        drain_updates(ts)
        # 2. mark to market, risk
        for key, lot in lots.items():
            lot["bars"] += 1
        equity = broker.equity()
        eq_hist.append(equity); ts_hist.append(ts)
        gross_hist.append(sum(abs(q) * last_close.get(s, 0.0) for s, q in broker.positions.items()))
        if trading:
            risk.update_equity(ts, equity)
        # 3. kill switch: liquidate at next open, stop trading
        if risk.killed and not killed_liq:
            killed_liq = True
            for (m, s), sl in list(risk.ledger.slices.items()):
                if abs(sl.qty) > 1e-12:
                    r = oms.flatten_slice(m, s, reason="kill switch")
                    if r:
                        r.events.insert(0, "t:kill switch")
        if risk.killed:
            continue
        # 4. software stops on the completed close
        for (m, s), stop in list(software_stop.items()):
            if s in today and abs(risk.ledger.slice(m, s).qty) > 1e-12 and not _slice_has_open_order(oms, m, s):
                c, side = last_close[s], 1 if risk.ledger.slice(m, s).qty > 0 else -1
                if (side == 1 and c <= stop) or (side == -1 and c >= stop):
                    r = oms.flatten_slice(m, s, reason=f"stop {stop:.2f} hit (close {c:.2f})")
                    if r:
                        r.kind = "exit"
                        r.events.insert(0, f"t:stop {stop:.2f} hit (close {c:.2f})")
        # 5. strategy signals -> intents -> allocator -> OMS (per intent, V1 sequential semantics)
        for s, i in today.items():
            a = arrays[s]
            ev = BarEvent(s, ts, a["open"][i], a["high"][i], a["low"][i], a["close"][i], a["volume"][i], "1d", ts.date(), True)
            features.on_bar(ev)
            snap = features.snapshot(s, ts)
            sl = risk.ledger.slice(strat_name, s)
            intents = adapters[s].on_event(ev, snap, SlicePos(sl.qty))
            if not intents or not trading:
                continue
            acct = broker.get_account()
            n_open = risk.ledger.open_positions() + sum(1 for v in risk.ledger.inflight.values() if v[2] != 0)
            for it in intents:
                if it.direction != 0:
                    ok, why = risk.manager.can_open(n_open)
                    if not ok:
                        oms.decisions.append({"ts": ts.isoformat(), "module": it.module_id, "symbol": s, "decision": "blocked", "detail": why})
                        continue
                views = [PositionView(x.symbol, x.module_id, x.qty, last_close.get(x.symbol, x.avg_price)) for x in risk.ledger.slices.values() if abs(x.qty) > 1e-12]
                alloc = allocator.allocate([it], views, acct.equity, cash=acct.cash)
                recs = oms.reconcile(alloc.targets, account=acct, prices={s: a["close"][i]}, session=ts.date(), market_open=True, bar_current=True)  # legacy: fills at next open
                for r in recs:
                    if r.status not in ("risk_rejected", "submit_failed"):
                        r.events.insert(0, f"t:{it.tag}")
    # end-of-run liquidation at the last close with exit costs (V1 semantics)
    last_ts = ts_hist[-1]
    for (m, s), sl in list(risk.ledger.slices.items()):
        if abs(sl.qty) > 1e-12 and s in last_close:
            side = 1 if sl.qty > 0 else -1
            f = FillEngine(fills).legacy_daily(-side, abs(sl.qty), last_close[s])
            broker.costs_paid += abs(sl.qty) * abs(f.price - last_close[s])
            broker.traded_notional += abs(sl.qty) * f.price
            broker.cash += side * abs(sl.qty) * f.price
            broker.positions.pop(s, None)
            lot = lots.pop((m, s), None)
            if lot:
                pnl = side * (f.price - lot["entry_price"]) * abs(sl.qty)
                trades.append(Trade(s, side, abs(sl.qty), lot["entry_ts"], lot["entry_price"], last_ts, f.price, pnl, pnl / (lot["entry_price"] * abs(sl.qty)),
                                    lot["bars"], lot["entry_reason"], "end of backtest"))
            sl.qty = 0.0
    eq_hist[-1] = broker.cash
    gross_hist[-1] = 0.0
    equity_s = pd.Series(eq_hist, index=pd.DatetimeIndex(ts_hist), name="equity")
    window = (equity_s.index >= t_start) & (equity_s.index <= t_end)
    eq_w = equity_s[window]
    gross_w = pd.Series(gross_hist, index=equity_s.index)[window]
    invested_days = int((gross_w > 0).sum())
    exposure = invested_days / max(len(eq_w), 1)
    metrics = compute_metrics(eq_w, trades, exposure=exposure)
    n_orders = sum(1 for o in broker.by_id.values() if o.filled_qty > 0) + sum(1 for t in trades if t.exit_reason == "end of backtest")
    metrics.update(costs_paid=broker.costs_paid, orders=n_orders, traded_notional=broker.traded_notional,
                   daily_halts=0, kill_switch=bool(risk.killed))
    res = BacktestResultV15(eq_w, trades, metrics, module_equity={strat_name: eq_w}, module_trades={strat_name: trades}, fills=broker.fills_log,
                            decisions=oms.decisions, costs_paid=broker.costs_paid, orders=n_orders, exposure=gross_w / eq_w, killed=risk.killed,
                            strategy=strat_name, params=params, symbols=symbols)
    if risk.killed:
        res.notes.append(f"KILL SWITCH TRIPPED: {risk.manager.state.kill_reason}. All positions liquidated; no trading afterwards.")
    if benchmark:
        bt = Backtester(strategy_factory, initial_cash=initial_cash)
        res.benchmark_equity = bt._buy_and_hold(data, eq_w.index)
        res.benchmark_metrics = compute_metrics(res.benchmark_equity)
    return res


def _slice_has_open_order(oms: OrderManager, m: str, s: str) -> bool:
    return any(o.module_id == m and o.symbol == s and o.is_open for o in oms.orders.values())


def _loc(ts, like) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None and like.tzinfo is not None:
        return ts.tz_localize(like.tzinfo)
    return ts


# =============================================================================================== minute mode
KIND_ORDER = ("pre_open", "session_open", "bar_close_1m", "bar_close_30m", "t1530", "t1550", "t1558", "session_close")
KIND_EVENTS = {"bar_close_1m": ("bar_close", "1m"), "bar_close_30m": ("bar_close", "30m")}


class Module:
    """Protocol for V1.5 modules: ``module_id``, ``symbols``, ``listens`` (names from KIND_ORDER), ``on_event``.

    ``on_event`` receives a ScheduleEvent (``bar_close`` carries ``timeframe`` "1m"/"30m"), the symbol's FeatureSnapshot
    and the module's own slice; it returns TradeIntents. When several listened kinds coincide on one bar the module is
    called once per kind, in KIND_ORDER; the OrderManager skips a second order for a slice that already has one open.
    Orders on one symbol from different modules in the same event are serialised by the one-open-order-per-symbol
    rule (RiskManager ``no_outstanding_order_conflict``): the later module's intent is blocked for that event.
    """
    module_id: str
    symbols: list[str]
    listens: set[str]

    def on_event(self, event, snapshot, position) -> list[TradeIntent]:  # pragma: no cover - protocol
        raise NotImplementedError


@dataclass
class MinuteRunConfig:
    initial_cash: float = 100_000.0
    fills: FillParams = field(default_factory=FillParams)
    whole_share_capable: bool = True
    symbol_kinds: dict[str, str] = field(default_factory=dict)
    sectors: dict[str, str] = field(default_factory=dict)
    quotes: dict[str, pd.DataFrame] | None = None       # optional per-symbol quote frames (bid, ask) indexed by ts
    run_id: str = "bt15"
    record_minute_equity: bool = False                  # False: one equity mark per session close (plus open)


def run_minute(modules: list[Module], bars_1m: dict[str, pd.DataFrame], *, calendar: SessionCalendar, policy: RiskPolicy,
               start: date, end: date, config: MinuteRunConfig | None = None, features: FeatureEngine | None = None) -> BacktestResultV15:
    cfg = config or MinuteRunConfig()
    bars_1m = {s.upper(): df for s, df in bars_1m.items()}
    symbols = list(bars_1m)
    updates: list[TradeUpdateEvent] = []
    fe = FillEngine(cfg.fills)
    broker = SimBroker(cfg.initial_cash, fills=fe, symbol_kinds=cfg.symbol_kinds, on_trade_update=updates.append,
                       whole_share_only=False)
    risk = RiskEngine(policy, sectors=cfg.sectors, allow_fractional=True)
    features = features or FeatureEngine(benchmark="SPY" if "SPY" in symbols else symbols[0], calendar=calendar)
    allocator = Allocator(policy, sectors=cfg.sectors, whole_share_capable=cfg.whole_share_capable)
    oms = OrderManager(broker, risk, policy, run_id=cfg.run_id, clock=lambda: broker.now, whole_share_capable=cfg.whole_share_capable,
                       symbol_kind=lambda s: cfg.symbol_kinds.get(s, "etf"))
    rng = random.Random(cfg.fills.seed)
    delay_queue: list[tuple[int, TradeIntent]] = []
    lots: dict[tuple[str, str], dict[str, Any]] = {}
    trades: list[Trade] = []
    module_trades: dict[str, list[Trade]] = {m.module_id: [] for m in modules}
    module_cash: dict[str, float] = {m.module_id: 0.0 for m in modules}
    module_eq: dict[str, list[tuple[datetime, float]]] = {m.module_id: [] for m in modules}
    attribution = {m.module_id: {"overnight": 0.0, "intraday": 0.0} for m in modules}
    eq_marks: list[tuple[datetime, float, float]] = []
    prices: dict[str, float] = {}
    spreads: dict[str, float] = {}
    stale: dict[str, float] = {}
    sessions = [s for s in calendar.sessions_between(start, end)]
    by_session = {s: {} for s in symbols}
    for s, df in bars_1m.items():
        idx = pd.DatetimeIndex(df.index).tz_convert(NY)
        for d, g in df.set_index(idx).groupby(idx.date):
            by_session[s][d] = g

    def module_value(mid: str) -> float:
        return module_cash[mid] + sum(sl.qty * prices.get(sl.symbol, sl.avg_price) for (m, _), sl in risk.ledger.slices.items() if m == mid)

    def drain(ts: datetime, session: date):
        while updates:
            ev = updates.pop(0)
            oms.on_trade_update(ev, session=session)
            rec = oms.orders.get(ev.client_order_id)
            if rec is None or ev.event not in ("fill", "partial_fill"):
                continue
            inc = ev.raw.get("leg_qty") or 0.0
            price = ev.price or rec.reference_price
            signed = inc if rec.side == "buy" else -inc
            module_cash[rec.module_id] -= signed * price
            key = (rec.module_id, rec.symbol)
            if rec.kind == "entry":
                lot = lots.setdefault(key, {"entry_ts": ts, "entry_price": price, "qty": 0.0, "bars": 0, "side": 1 if rec.side == "buy" else -1, "entry_reason": rec.style})
                lot["entry_price"] = (lot["entry_price"] * lot["qty"] + price * inc) / (lot["qty"] + inc) if lot["qty"] + inc > 0 else price
                lot["qty"] += inc
            elif key in lots:
                lot = lots[key]
                q = min(inc, lot["qty"])
                pnl = lot["side"] * (price - lot["entry_price"]) * q
                tr = Trade(rec.symbol, lot["side"], q, lot["entry_ts"], lot["entry_price"], ts, price, pnl, pnl / (lot["entry_price"] * q) if q else 0.0,
                           lot["bars"], lot["entry_reason"], rec.kind)
                trades.append(tr)
                module_trades[rec.module_id].append(tr)
                lot["qty"] -= q
                if lot["qty"] <= 1e-9:
                    lots.pop(key, None)

    def dispatch(ts: datetime, session: date, kinds: set[str]):
        """Deliver one ScheduleEvent per (module, listened kind) in KIND_ORDER; modules see kinds, never raw bars."""
        acct = broker.get_account()
        new_intents: list[TradeIntent] = []
        for mod in modules:
            hit = [k for k in KIND_ORDER if k in kinds and k in mod.listens]
            if not hit:
                continue
            for sym in mod.symbols:
                if sym not in symbols:
                    continue
                snap = features.snapshot(sym, ts)
                sl = risk.ledger.slice(mod.module_id, sym)
                emitted: list[TradeIntent] = []
                for k in hit:
                    kind, tf = KIND_EVENTS.get(k, (k, None))
                    emitted.extend(mod.on_event(ScheduleEvent(kind, ts, session, tf), snap, SlicePos(sl.qty)))
                for it in emitted:
                    if cfg.fills.drop_signal_fraction > 0 and it.direction != 0 and rng.random() < cfg.fills.drop_signal_fraction:
                        oms.decisions.append({"ts": ts.isoformat(), "module": it.module_id, "symbol": sym, "decision": "dropped(stress)", "detail": ""})
                        continue
                    new_intents.append(it)
        if cfg.fills.execution_delay_bars > 0:
            delay_queue.extend((cfg.fills.execution_delay_bars, it) for it in new_intents)
            new_intents = []
        ready = []
        for i in range(len(delay_queue) - 1, -1, -1):
            n, it = delay_queue[i]
            if n <= 0:
                ready.append(it); delay_queue.pop(i)
            else:
                delay_queue[i] = (n - 1, it)
        new_intents.extend(reversed(ready))
        if not new_intents:
            return
        admitted = []
        recent = [x.date for x in calendar.sessions_between(session - timedelta(days=9), session)]
        for it in new_intents:
            d = risk.admit(it, spread_bps=spreads.get(it.symbol), stale_seconds=stale.get(it.symbol), is_etf=cfg.symbol_kinds.get(it.symbol, "etf") == "etf",
                           account=acct, recent_sessions=recent)
            if d.approved:
                mult = risk.budget_multiplier(it.module_id)
                admitted.append(it if mult >= 1.0 else TradeIntent(**{**it.__dict__, "risk_budget_pct": it.risk_budget_pct * mult}))
            else:
                oms.decisions.append({"ts": ts.isoformat(), "module": it.module_id, "symbol": it.symbol, "decision": "not_admitted", "detail": f"{d.code}: {d.detail}"})
        if not admitted:
            return
        views = [PositionView(x.symbol, x.module_id, x.qty, prices.get(x.symbol, x.avg_price)) for x in risk.ledger.slices.values() if abs(x.qty) > 1e-12]
        alloc = allocator.allocate(admitted, views, acct.equity, cash=acct.cash)
        oms.reconcile(alloc.targets, account=acct, prices=prices, session=session, market_open=True, spreads=spreads, stale=stale)

    killed_handled = False
    for sess in sessions:
        d = sess.date
        day_bars = {s: by_session[s].get(d) for s in symbols}
        if all(v is None or v.empty for v in day_bars.values()):
            continue
        minutes = sorted(set().union(*[set(v.index) for v in day_bars.values() if v is not None]))
        pre = datetime.combine(d, time(9, 0), NY)
        broker.now = pre
        oms.on_schedule(ScheduleEvent("pre_open", pre, d), prices=prices)
        dispatch(pre, d, {"pre_open"})
        # opening auction: official open = first minute bar's open
        # attribution: overnight = prev close -> official open on positions HELD into the session; everything else
        # (auction fills and their costs, intraday moves) is intraday, so overnight + intraday = the module's P&L
        prev_values = {m: module_value(m) for m in module_cash}
        held = {(m, s): sl.qty for (m, s), sl in risk.ledger.slices.items() if abs(sl.qty) > 1e-12}
        prev_px = dict(prices)
        for s, v in day_bars.items():
            if v is not None and not v.empty:
                broker.session_open(s, float(v["open"].iloc[0]), sess.open)
                prices[s] = float(v["open"].iloc[0])
        drain(sess.open, d)
        session_base = {}
        for m in module_cash:
            on = sum(q * (prices[s] - prev_px.get(s, prices[s])) for (mm, s), q in held.items() if mm == m)
            attribution[m]["overnight"] += on
            session_base[m] = prev_values[m] + on
        eq_marks.append((sess.open, broker.equity(), sum(abs(q) * prices.get(s, 0) for s, q in broker.positions.items())))
        dispatch(sess.open, d, {"session_open"})
        acc30: dict[str, list] = {s: [] for s in symbols}
        for ts in minutes:
            ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            bars_now: dict[str, BarEvent] = {}
            for s, v in day_bars.items():
                if v is None or ts not in v.index:
                    continue
                r = v.loc[ts]
                ev = BarEvent(s, ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]), "1m", d,
                              is_session_end=(ts + timedelta(minutes=1) >= sess.close), vwap=float(r["vwap"]) if "vwap" in v and pd.notna(r["vwap"]) else None,
                              backfilled=bool(r["backfilled"]) if "backfilled" in v else False)
                bars_now[s] = ev
                broker.step(ev)          # fills for orders decided on earlier bars
                prices[s] = ev.close
                stale[s] = 0.0
                if cfg.quotes and s in cfg.quotes:
                    q = cfg.quotes[s]
                    sub = q[(q.index <= ts)]
                    if len(sub):
                        last = sub.iloc[-1]
                        qe = QuoteEvent(s, sub.index[-1].to_pydatetime(), float(last["bid"]), float(last["ask"]))
                        features.on_quote(qe)
                        spreads[s] = qe.spread_bps
                        broker.set_quote_spread(s, qe.spread_bps)
            drain(ts, d)
            for s, ev in bars_now.items():
                features.on_bar(ev)
                acc30[s].append(ev)
            oms.tick(prices=prices, spreads=spreads)
            kinds = {"bar_close_1m"}
            end_of_bucket = ((ts - sess.open).total_seconds() // 60 + 1) % 30 == 0 or (ts + timedelta(minutes=1) >= sess.close)
            # schedule kinds are relative to the session close so early closes (13:00) get the same 30/10/2-minute marks;
            # ts is the START of the just-completed bar, so t1558 fires on the 15:57 bar (decision at 15:58 wall time)
            to_close = int((sess.close - ts).total_seconds() // 60)
            if end_of_bucket:
                for s, acc in acc30.items():
                    if acc:
                        b0 = acc[0]
                        ev30 = BarEvent(s, b0.ts, b0.open, max(x.high for x in acc), min(x.low for x in acc), acc[-1].close, sum(x.volume for x in acc), "30m", d,
                                        is_session_end=acc[-1].is_session_end, partial=len(acc) < 30 and acc[-1].is_session_end)
                        features.on_bar(ev30)
                        acc30[s] = []
                kinds.add("bar_close_30m")
                if to_close == 31:
                    kinds.add("t1530")
            if to_close == 11:
                kinds.add("t1550")
            if to_close == 3:
                kinds.add("t1558")
            for k in ("t1530", "t1550", "t1558"):
                if k in kinds:
                    oms.on_schedule(ScheduleEvent(k, ts, d), prices=prices)
            dispatch(ts, d, kinds)
            # risk on minute marks
            eq = broker.equity()
            for ev_r in risk.update_equity(ts, eq):
                if ev_r.kind == "kill_switch" and not killed_handled:
                    killed_handled = True
                    oms.cancel_non_protective()
                    oms.flatten_all(reason="kill switch")
            if cfg.record_minute_equity:
                eq_marks.append((ts, eq, sum(abs(q) * prices.get(s, 0) for s, q in broker.positions.items())))
            for lot in lots.values():
                lot["bars"] += 1
        # closing auction
        for s, v in day_bars.items():
            if v is not None and not v.empty:
                broker.session_close(s, float(v["close"].iloc[-1]), sess.close)
                prices[s] = float(v["close"].iloc[-1])
        drain(sess.close, d)
        broker.end_of_day(sess.close)
        drain(sess.close, d)
        for m in module_cash:
            attribution[m]["intraday"] += module_value(m) - session_base[m]
            module_eq[m].append((sess.close, module_value(m)))
        eq_marks.append((sess.close, broker.equity(), sum(abs(q) * prices.get(s, 0) for s, q in broker.positions.items())))
        dispatch(sess.close, d, {"session_close"})
        drain(sess.close, d)
    idx = pd.DatetimeIndex([m[0] for m in eq_marks])
    equity = pd.Series([m[1] for m in eq_marks], index=idx, name="equity")
    gross = pd.Series([m[2] for m in eq_marks], index=idx)
    daily_eq = equity[[ts.time() >= time(15, 59) or ts.time() == time(16, 0) or ts.time() == time(13, 0) for ts in equity.index]]
    daily_eq = daily_eq if len(daily_eq) >= 2 else equity
    metrics = compute_metrics(daily_eq, trades, exposure=float((gross > 0).mean()) if len(gross) else 0.0)
    metrics.update(costs_paid=broker.costs_paid, orders=len(broker.by_id), traded_notional=broker.traded_notional, kill_switch=bool(risk.killed))
    res = BacktestResultV15(daily_eq, trades, metrics, module_equity={m: pd.Series([v for _, v in xs], index=pd.DatetimeIndex([t for t, _ in xs])) for m, xs in module_eq.items()},
                            module_trades=module_trades, fills=broker.fills_log, decisions=oms.decisions, costs_paid=broker.costs_paid, orders=len(broker.by_id),
                            exposure=gross / equity, attribution=attribution, killed=risk.killed, strategy="+".join(m.module_id for m in modules),
                            symbols=symbols)
    if risk.killed:
        res.notes.append(f"KILL SWITCH TRIPPED: {risk.manager.state.kill_reason}")
    return res
