"""Autonomous runtime daemon (spec §12): boot -> reconcile -> subscribe -> scheduler -> cycles -> graceful shutdown.

Paper by default. ``env="live"`` additionally requires TRADING_ENV=live, the --live flag, LIVE_AUTONOMOUS_TRADING=true,
the interlock armed with the fingerprint of the loaded live policy, and every module in that policy promoted. The
daemon never raises a limit, never places an order outside the RiskEngine -> Allocator -> OrderManager path, and
persists every step to the ExecutionStore before and after the broker call. Nothing here imports bot.research.
"""
from __future__ import annotations

import logging
import signal
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from bot.core.bus import EventBus
from bot.core.events import BarEvent, QuoteEvent, ScheduleEvent, TradeUpdateEvent
from bot.core.policy import RiskPolicy
from bot.data.calendar import NY
from bot.execution.oms import OrderManager
from bot.execution.reconcile import reconcile
from bot.execution.store import ExecutionStore
from bot.features.engine import FeatureEngine, load_earnings
from bot.portfolio.allocator import Allocator
from bot.portfolio.dispatch import collect_intents, dispatch_intents, recent_sessions
from bot.risk.manager import RiskState
from bot.risk.policy_engine import RiskEngine
from bot.runtime.scheduler import Scheduler

log = logging.getLogger(__name__)
RECONCILE_EVERY = timedelta(minutes=5)
HEARTBEAT_EVERY = timedelta(seconds=30)
ACCOUNT_CACHE = timedelta(seconds=10)


class DaemonRefused(PermissionError):
    """Boot refused: a gate failed. Nothing was started."""


class Daemon:
    def __init__(self, settings, *, env: str, policy_path: str | Path | None = None, modules: list | None = None, broker=None, store: ExecutionStore | None = None,
                 hub=None, trade_updates=None, calendar=None, alerter=None, clock: Callable[[], datetime] | None = None, cli_live_flag: bool = False,
                 run_id: str | None = None, universe=None, bar_store=None, provider=None, halt_flag_path: str | Path | None = None,
                 whole_share_capable: bool | None = None, bus: EventBus | None = None, interlock=None):
        self.settings, self.env, self.cli_live_flag = settings, env, cli_live_flag
        self.policy_path = Path(policy_path) if policy_path else (settings.live_policy_path if env == "live" else settings.paper_policy_path)
        self.run_id = run_id or env
        self.broker, self.store, self.hub, self.tu, self.calendar = broker, store, hub, trade_updates, calendar
        self.alerter = alerter
        self.clock = clock or (lambda: datetime.now(NY))
        self.universe, self.bar_store, self.provider = universe, bar_store, provider
        self.halt_flag = Path(halt_flag_path) if halt_flag_path else Path(settings.state_dir) / f"{self.run_id}.halt"
        self.bus = bus or EventBus()
        self.modules = modules
        self.whole_share_capable = whole_share_capable
        self.interlock = interlock
        self.policy: RiskPolicy | None = None
        self.risk: RiskEngine | None = None
        self.oms: OrderManager | None = None
        self.features: FeatureEngine | None = None
        self.allocator: Allocator | None = None
        self.scheduler: Scheduler | None = None
        self.symbols: list[str] = []
        self.symbol_kinds: dict[str, str] = {}
        self.prices: dict[str, float] = {}
        self.spreads: dict[str, float] = {}
        self.quote_ts: dict[str, datetime] = {}
        self.last_bar_ts: dict[str, datetime] = {}
        self._stop = False
        self._acct = None
        self._acct_at: datetime | None = None
        self._last_reconcile: datetime | None = None
        self._last_heartbeat: datetime | None = None
        self._session_marked: date | None = None
        self._booted_at: datetime | None = None
        self._trades_seen = 0
        self._killed_handled = False
        self._lock = threading.RLock()
        self.exit_code = 0

    # ------------------------------------------------------------------ alerts
    def alert(self, title: str, msg: str = "", level: str = "info") -> None:
        if self.alerter is not None:
            try:
                self.alerter.send(f"[{self.env.upper()}] {title}", msg, level=level)
            except Exception as e:  # noqa: BLE001
                log.warning("alert failed: %s", e)
        log.log(logging.CRITICAL if level == "critical" else logging.WARNING if level == "warning" else logging.INFO, "%s: %s", title, msg)

    # -------------------------------------------------------------------- boot
    def boot(self) -> None:
        s = self.settings
        now = self.clock()
        self.policy = RiskPolicy.load(self.policy_path, require_promotions=(self.env == "live"))
        fp = self.policy.fingerprint()
        if self.env == "live":
            if s.trading_env != "live":
                raise DaemonRefused("env=live but TRADING_ENV is not 'live'")
            if not self.cli_live_flag:
                raise DaemonRefused("env=live requires the --live flag")
            if not s.live_autonomous_trading:
                raise DaemonRefused("LIVE_AUTONOMOUS_TRADING=false: the live daemon is disabled (paper is unaffected)")
            il = self.interlock
            if il is None:
                from bot.execution.interlock import LiveInterlock
                il = LiveInterlock(s)
            armed, why = il.is_armed(policy_fingerprint=fp)
            if not armed:
                raise DaemonRefused(f"interlock: {why}")
        if self.broker is None:
            from bot.execution.broker import AlpacaBroker
            self.broker = AlpacaBroker.for_env(s, self.env)
        ok, why = self.broker.verify_account_env()
        if not ok:
            raise DaemonRefused(f"account/env mismatch: {why}")
        acct = self.account(force=True)
        if self.store is None:
            self.store = ExecutionStore(Path(s.state_dir) / f"{self.run_id}.sqlite")
        stored = self.store.meta("policy_fingerprint")
        if stored and stored != fp:
            if self.env == "live":
                raise DaemonRefused(f"policy fingerprint changed since the last live run ({stored[:12]}… -> {fp[:12]}…); re-arm")
            self.store.add_decision({"ts": now.isoformat(), "decision": "policy_changed", "detail": f"{stored[:12]}… -> {fp[:12]}…"}, kind="operator")
            self.alert("policy changed since last run", f"{stored[:12]}… -> {fp[:12]}…", "warning")
        self.store.set_meta("policy_fingerprint", fp)
        self.store.set_meta("env", self.env)
        self.store.set_meta("run_id", self.run_id)
        # universe / symbols / modules
        if self.universe is None:
            from bot.data.universe import Universe
            self.universe = Universe.load(s.universe_path)
        if self.calendar is None:
            from bot.data.sessions import SessionCalendar
            self.calendar = SessionCalendar.from_store(self.bar_store) if self.bar_store is not None else SessionCalendar()
            if self.bar_store is not None:
                try:
                    self.calendar.sync_from_broker(self.broker, self.bar_store, now.date() - timedelta(days=30), now.date() + timedelta(days=60))
                except Exception as e:  # noqa: BLE001
                    log.warning("calendar sync failed (%s); using cached/rule calendar", type(e).__name__)
        whole = self.whole_share_capable if self.whole_share_capable is not None else acct.equity >= s.whole_share_min_equity
        if self.modules is None:
            self.modules = self._build_modules(whole)
        syms = []
        for m in self.modules:
            syms += [x for x in m.symbols if x not in syms]
        self.symbols = syms
        self.symbol_kinds = {x: ("etf" if x in self.universe.tier1 + self.universe.tier2 else "stock") for x in syms}
        # risk / oms / features / allocator
        rs = self.store.load_risk()
        self.risk = RiskEngine(self.policy, state=RiskState.from_dict(rs) if rs else None, sectors=self.universe.sectors, allow_fractional=s.allow_fractional)
        for mod, (mult, _) in self.store.throttles().items():
            self.risk.throttles[mod] = mult
        self.oms = OrderManager(self.broker, self.risk, self.policy, run_id=self.run_id, clock=self.clock, whole_share_capable=whole,
                                on_alert=lambda t, m: self.alert(t, m, "warning"), symbol_kind=lambda x: self.symbol_kinds.get(x, "etf"), store=self.store)
        n = self.oms.restore()
        self.features = FeatureEngine(benchmark="SPY" if "SPY" in syms else (syms[0] if syms else "SPY"), earnings=load_earnings(), calendar=self.calendar)
        self.allocator = Allocator(self.policy, sectors=self.universe.sectors, whole_share_capable=whole)
        self.scheduler = Scheduler(self.calendar)
        self.scheduler.due(now, catch_up_from=now)          # mark everything earlier today as fired: never replay the morning
        self._booted_at = now
        self.risk.update_equity(now, acct.equity)          # peak / day-start equity observed at boot
        self.warm_features()
        self.store.heartbeat("daemon", now, "boot")
        self._mark_session(now)
        self.alert("daemon started", f"policy {self.policy_path} {fp[:16]}… · modules {[m.module_id for m in self.modules]} · symbols {syms} · "
                   f"account …{acct.account_number[-4:]} equity {acct.equity:,.2f} · restored {n} orders · whole_shares={whole}")
        self.reconcile_now(now, first=True)

    def _build_modules(self, whole: bool) -> list:
        from bot.strategies.v15 import MODULES
        out = []
        for mid in self.policy.allowed_modules:
            cls = MODULES.get(mid)
            if cls is None:
                continue          # legacy baselines run through `python -m bot trade`, not the daemon
            symbols = self.universe.tier3 if mid == "M3" else [x for x in ("SPY", "QQQ") if x in self.universe.symbols]
            if not symbols:
                log.warning("module %s has no symbols in the universe; skipped", mid)
                continue
            out.append(cls(symbols, fractional=not whole))
        return out

    def warm_features(self, sessions: int = 300) -> None:
        """Feed the last ``sessions`` daily bars and today's cached minute bars so features are ready at boot."""
        if self.bar_store is None or self.features is None:
            return
        now = self.clock()
        for sym in self.symbols:
            try:
                df = self.bar_store.get_bars(sym, now.date() - timedelta(days=int(sessions * 1.6)), now.date())
            except Exception as e:  # noqa: BLE001
                log.warning("warm-up daily bars for %s failed: %s", sym, e)
                continue
            for ts, r in df.iterrows():
                d = ts.date()
                if d >= now.date():
                    continue
                sess = self.calendar.session(d)
                if sess is None:
                    continue
                self.features.on_bar(BarEvent(sym, sess.close, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]), "1d", d, True))
                self.prices.setdefault(sym, float(r["close"]))

    # ---------------------------------------------------------------- helpers
    def account(self, force: bool = False):
        now = self.clock()
        if force or self._acct is None or self._acct_at is None or now - self._acct_at > ACCOUNT_CACHE:
            self._acct, self._acct_at = self.broker.get_account(), now
        return self._acct

    def _mark_session(self, now: datetime) -> None:
        d = now.date()
        if self._session_marked != d and self.calendar.session(d) is not None:
            self._session_marked = d
            self.store.heartbeat("daemon_session", now, d.isoformat())

    def halted_by_watchdog(self) -> str | None:
        if self.halt_flag.exists():
            try:
                return self.halt_flag.read_text(encoding="utf-8").strip() or "halt flag present"
            except OSError:
                return "halt flag present"
        return None

    def stale_seconds(self) -> dict[str, float]:
        """Seconds since the end of the last 1-minute bar the daemon itself processed (hub or bus); unknown symbols are
        left out, which the RiskEngine treats as 'no measurement' (admission passes, the OMS/RiskManager gate still runs)."""
        now = self.clock()
        out = {}
        for s in self.symbols:
            last = self.last_bar_ts.get(s)
            age = ((now - (last + timedelta(minutes=1))).total_seconds()) if last else (self.hub.last_bar_age(s, now) if self.hub is not None else None)
            if age is not None:
                out[s] = max(age, 0.0)
        return out

    def market_open(self, now: datetime | None = None) -> bool:
        now = now or self.clock()
        return self.calendar.is_regular_hours(now)

    # -------------------------------------------------------------- reconcile
    def reconcile_now(self, now: datetime | None = None, *, first: bool = False) -> None:
        now = now or self.clock()
        with self._lock:
            rep = reconcile(broker=self.broker, oms=self.oms, policy=self.policy, prices=self.prices, session=self._session_date(now), now=now, store=self.store,
                            alert=lambda t, m: self.alert(t, m, "warning"))
            self._last_reconcile = now
            self.store.save_risk(self.risk.manager.state)
            if first:
                # restart safety: positions without a live protective order get one now (policy permitting)
                for (m, s), sl in list(self.risk.ledger.slices.items()):
                    if abs(sl.qty) > 1e-12 and (m, s) not in self.oms.protective and not self.oms.software_stops and self.policy.require_broker_protection_overnight:
                        rec = self.oms.orders.get(sl.protective_order_id or "")
                        stop = rec.stop_price if rec is not None and rec.stop_price else None
                        if stop is None:
                            px = self.prices.get(s, sl.avg_price)
                            stop = px * (0.95 if sl.qty > 0 else 1.05)
                        self.oms.place_protective(m, s, stop)
                        self.alert("protection re-attached after restart", f"{s} {m}: stop {stop:.2f}", "warning")
            if self.hub is not None:
                self.hub.set_quote_symbols({s for (_, s), sl in self.risk.ledger.slices.items() if abs(sl.qty) > 1e-12} | {o.symbol for o in self.oms.orders.values() if o.is_open})
        if not rep.clean:
            log.warning("reconciliation: %d discrepancies", len(rep.discrepancies))

    def _session_date(self, now: datetime) -> date:
        return self.calendar.last_completed_session(now).date if not self.calendar.session(now.date()) else now.date()

    # ------------------------------------------------------------------ cycle
    def start_streams(self) -> None:
        if self.hub is not None:
            self.hub.start()
        if self.tu is not None:
            self.tu.on_reconnect = lambda: self.reconcile_now()
            self.tu.start()

    def handle(self, ev: Any) -> None:
        with self._lock:
            if isinstance(ev, BarEvent):
                self._on_bar(ev)
            elif isinstance(ev, QuoteEvent):
                self.spreads[ev.symbol] = ev.spread_bps
                self.quote_ts[ev.symbol] = ev.ts
                self.features.on_quote(ev)
            elif isinstance(ev, TradeUpdateEvent):
                self._on_trade_update(ev)
            elif isinstance(ev, ScheduleEvent):
                self._on_schedule(ev)

    def _dispatch(self, kinds: set[str], now: datetime) -> None:
        if self.risk.killed:
            return
        halted = self.halted_by_watchdog()
        if halted and not self.risk.halt_entries:
            self.risk.halt_entries, self.risk.halt_reason = True, f"watchdog halt: {halted}"
            self.alert("entries halted by the watchdog", halted, "critical")
        elif not halted and self.risk.halt_entries and self.risk.halt_reason.startswith("watchdog halt"):
            self.risk.halt_entries, self.risk.halt_reason = False, ""
        acct = self.account()
        session = self._session_date(now)
        intents = collect_intents(self.modules, kinds=kinds, ts=now, session=session, symbols=self.symbols, features=self.features, risk=self.risk,
                                  prices=self.prices, equity=acct.equity)
        if not intents:
            return
        stale = self.stale_seconds()
        before = set(self.oms.orders)
        dispatch_intents(intents, ts=now, session=session, account=acct, risk=self.risk, allocator=self.allocator, oms=self.oms, prices=self.prices,
                         spreads=self.spreads, stale=stale, symbol_kinds=self.symbol_kinds, recent_sessions=recent_sessions(self.calendar, session),
                         market_open=self.market_open(now), shortable=self._shortable)
        for cid in set(self.oms.orders) - before:
            rec = self.oms.orders[cid]
            if rec.status in ("risk_rejected", "submit_failed"):
                continue
            self.alert(f"{rec.kind}: {rec.symbol} {rec.module_id}", f"{rec.side} {rec.qty:g} {rec.style} {rec.tif} ref {rec.reference_price:.2f}"
                       + (f" stop {rec.stop_price:.2f}" if rec.stop_price else "") + f" · {rec.risk['code'] if rec.risk else ''}")
        if self.hub is not None:
            self.hub.set_quote_symbols({s for (_, s), sl in self.risk.ledger.slices.items() if abs(sl.qty) > 1e-12} | {o.symbol for o in self.oms.orders.values() if o.is_open})

    def _shortable(self, symbol: str) -> bool:
        try:
            return bool(self.broker.get_asset(symbol).shortable)
        except Exception:  # noqa: BLE001
            return False

    def _on_bar(self, ev: BarEvent) -> None:
        self.features.on_bar(ev)
        if ev.timeframe == "1m":
            self.prices[ev.symbol] = ev.close
            self.last_bar_ts[ev.symbol] = ev.ts
            now = self.clock()
            self.oms.tick(prices=self.prices, spreads=self.spreads)
            self._dispatch({"bar_close_1m"}, ev.ts)
            self._risk_mark(now)
        elif ev.timeframe == "30m":
            self._dispatch({"bar_close_30m"}, ev.ts)

    def _risk_mark(self, now: datetime) -> None:
        acct = self.account(force=True)          # one account read per bar: the kill switch must see the real equity
        for r in self.risk.update_equity(now, acct.equity):
            if r.kind == "daily_halt":
                self.alert("daily loss halt", r.detail, "critical")
            if r.kind == "kill_switch" and not self._killed_handled:
                self._killed_handled = True
                self.alert("KILL SWITCH", r.detail, "critical")
                self.oms.cancel_non_protective()
                self.oms.flatten_all(reason="kill switch")
        self.store.save_risk(self.risk.manager.state)

    def _on_trade_update(self, ev: TradeUpdateEvent) -> None:
        before_trades = len(self.oms.trades)
        self.oms.on_trade_update(ev, session=self._session_date(self.clock()))
        rec = self.oms.orders.get(ev.client_order_id) or self.oms.by_broker_id.get(ev.order_id)
        if rec is None:
            return
        if ev.event in ("fill", "partial_fill") and ev.price:
            slip = (ev.price - rec.reference_price) / rec.reference_price * 1e4 * (1 if rec.side == "buy" else -1) if rec.reference_price else 0.0
            self.alert(f"{ev.event}: {rec.symbol} {rec.module_id}", f"{rec.side} {ev.filled_qty:g}/{rec.qty:g} @ {ev.price:.2f} (slippage {slip:+.1f} bps vs ref {rec.reference_price:.2f})",
                       "info" if ev.event == "fill" else "warning")
        elif ev.event in ("rejected", "canceled", "expired"):
            self.alert(f"{ev.event}: {rec.symbol} {rec.module_id}", f"{rec.kind} {rec.side} {rec.qty:g}", "warning" if ev.event == "rejected" else "info")
        for t in self.oms.trades[before_trades:]:
            self.alert(f"exit: {t['symbol']} {t['module']}", f"P&L {t['pnl']:+.2f} ({t['exit_reason']})", "info")
        for mod, mult in self.risk.throttles.items():
            if mult < 1.0 and mod not in self.store.throttles():
                self.store.set_throttle(mod, mult, "last 60 trades net negative")
                self.alert(f"throttle: {mod}", "risk budget halved; `python -m bot risk unthrottle --module " + mod + "` restores it", "warning")

    def _on_schedule(self, ev: ScheduleEvent) -> None:
        now = ev.ts
        if ev.kind == "pre_open":
            self.oms.on_schedule(ev, prices=self.prices)
            if ev.timeframe != "protect":
                self._dispatch({"pre_open"}, now)
            return
        if ev.kind in ("t1530", "t1550", "t1558"):
            self.oms.on_schedule(ev, prices=self.prices)
        if ev.kind == "post_close":
            self.daily_summary(now)
            return
        self._dispatch({ev.kind}, now)
        if ev.kind == "session_close":
            self.store.save_slices(self.risk.ledger)

    def daily_summary(self, now: datetime) -> None:
        acct = self.account(force=True)
        st = self.risk.manager.state
        unprotected = [f"{s}({m})" for (m, s), sl in self.risk.ledger.slices.items() if abs(sl.qty) > 1e-12 and (sl.unprotected or sl.unprotected_overnight)]
        trades = [t for t in self.oms.trades if str(t.get("session")) == now.date().isoformat()]
        pnl = sum(t["pnl"] for t in trades)
        self.alert("daily summary", f"equity {acct.equity:,.2f} (day start {st.day_start_equity:,.2f}, peak {st.peak_equity:,.2f}) · trades today {len(trades)} P&L {pnl:+.2f} · "
                   f"orders {len(self.oms.orders)} · throttles {self.risk.throttles or 'none'} · unprotected overnight {unprotected or 'none'} · "
                   f"halted={self.risk.halt_entries} killed={self.risk.killed}")
        self.store.set_meta("last_summary", now.isoformat())

    # ------------------------------------------------------------------- loop
    def _on_signal(self, *_):
        log.info("shutdown requested; finishing the current cycle")
        self._stop = True

    def run(self, *, max_cycles: int | None = None) -> int:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        cycles = 0
        try:
            self.start_streams()
            while not self._stop:
                self.cycle()
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
        except Exception as e:  # noqa: BLE001
            log.exception("daemon crashed: %s", e)
            self.alert("daemon crashed", f"{type(e).__name__}: {e}", "critical")
            self.exit_code = 1
            raise
        finally:
            self.shutdown()
        return self.exit_code

    def cycle(self, wait: float = 1.0) -> int:
        """One loop iteration: drain the bus, fire due schedule events, periodic reconcile and heartbeat."""
        n = 0
        ev = self.bus.poll(wait)
        while ev is not None:
            self.handle(ev)
            n += 1
            ev = self.bus.poll(0.0) if n < 10_000 else None
        now = self.clock()
        for se in self.scheduler.due(now):
            self.handle(se)
            n += 1
        if self._last_reconcile is None or now - self._last_reconcile >= RECONCILE_EVERY:
            self.reconcile_now(now)
        if self._last_heartbeat is None or now - self._last_heartbeat >= HEARTBEAT_EVERY:
            self.store.heartbeat("daemon", now, f"queue={len(self.bus)} hub={'up' if self.hub is not None and self.hub.connected else 'n/a'} "
                                                f"tu={'up' if self.tu is not None and self.tu.connected else 'n/a'}")
            self._last_heartbeat = now
            self._mark_session(now)
        return n

    def shutdown(self) -> None:
        now = self.clock()
        try:
            with self._lock:
                n = self.oms.cancel_non_protective()
                self.store.save_slices(self.risk.ledger)
                self.store.save_risk(self.risk.manager.state)
                self.store.heartbeat("daemon", now, "shutdown")
        except Exception as e:  # noqa: BLE001
            log.error("shutdown persistence failed: %s", e)
            self.exit_code = self.exit_code or 2
            n = -1
        for c in (self.hub, self.tu):
            if c is not None:
                try:
                    c.stop()
                except Exception:  # noqa: BLE001
                    pass
        self.alert("daemon stopped", f"cancelled {n} pending entries; protective orders left in place; state persisted; exit {self.exit_code}")
        _time.sleep(0)
