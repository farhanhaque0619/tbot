"""Watchdog (spec §12): an independent process that checks the daemon and the account every 30 s and escalates only as
configured in config/watchdog.yaml: alert -> halt entries (flag file the daemon honours) -> cancel pending entries ->
flatten per policy. It has read-only broker access plus cancel/close, imports nothing from bot.strategies, and never
touches a protective leg except by flattening.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from bot.data.calendar import NY

log = logging.getLogger(__name__)
ACTIONS = ("alert", "halt_entries", "cancel_pending_entries", "flatten")


@dataclass
class Finding:
    condition: str
    detail: str
    severity: str = "warning"        # warning | critical


@dataclass
class WatchdogReport:
    ts: datetime
    findings: list[Finding] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings


def load_config(path: str | Path = "config/watchdog.yaml") -> dict[str, Any]:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    raw = raw or {}
    raw.setdefault("interval_seconds", 30)
    raw.setdefault("thresholds", {})
    raw.setdefault("actions", ["alert"])
    raw.setdefault("flatten_on", [])
    raw.setdefault("halt_on", [])
    raw.setdefault("alert_on", [])
    return raw


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Watchdog:
    def __init__(self, config: dict[str, Any], *, broker, store, calendar, alert: Callable[[str, str, str], None] | None = None,
                 clock: Callable[[], datetime] | None = None, halt_flag_path: str | Path, policy=None):
        self.cfg = config
        self.broker, self.store, self.calendar, self.policy = broker, store, calendar, policy
        self.alert = alert or (lambda t, m, level="warning": None)
        self.clock = clock or (lambda: datetime.now(NY))
        self.halt_flag = Path(halt_flag_path)
        self.th = config.get("thresholds", {})
        self.enabled = [a for a in ACTIONS if a in config.get("actions", [])]
        self._stop = False
        self._last_broker_ok: datetime | None = None
        self.flattened_for: set[str] = set()

    # ---------------------------------------------------------------- checks
    def check(self) -> WatchdogReport:
        now = self.clock()
        rep = WatchdogReport(now)
        th = self.th
        hb = self.store.heartbeats()
        # 1. daemon heartbeat age
        d = hb.get("daemon")
        age = (now - _parse(d[0])).total_seconds() if d else None
        if age is None:
            rep.findings.append(Finding("heartbeat_missing", "no daemon heartbeat recorded", "warning"))
        elif age >= th.get("heartbeat_dead_seconds", 300):
            rep.findings.append(Finding("heartbeat_stale_300s", f"daemon heartbeat {age:.0f}s old (last: {d[1]})", "critical"))
        elif age >= th.get("heartbeat_stale_seconds", 120):
            rep.findings.append(Finding("heartbeat_stale", f"daemon heartbeat {age:.0f}s old", "warning"))
        regular = self.calendar.is_regular_hours(now)
        # 2. market-feed freshness (regular hours only)
        if regular:
            lb = _parse(self.store.meta("last_bar_ts"))
            bar_age = (now - lb).total_seconds() if lb else None
            if bar_age is None or bar_age >= th.get("feed_stale_seconds", 180):
                rep.findings.append(Finding("feed_stale", f"last market-data bar {'never' if bar_age is None else f'{bar_age:.0f}s ago'}", "warning"))
        # 3. trade-updates connection
        tu = hb.get("trade_updates")
        if tu is None or tu[1] != "connected":
            if regular or tu is not None:
                rep.findings.append(Finding("trade_updates_stale", f"trade updates: {tu[1] if tu else 'never connected'}", "warning"))
        elif regular and (now - _parse(tu[0])).total_seconds() >= th.get("trade_updates_stale_seconds", 600) and not self._tu_recent_events(now):
            pass   # a quiet but connected stream is fine; events prove liveness only when there are orders
        # 4. broker reachability
        positions = None
        try:
            self.broker.get_clock()
            acct = self.broker.get_account()
            positions = self.broker.get_positions()
            open_orders = self.broker.get_open_orders()
            self._last_broker_ok = now
        except Exception as e:  # noqa: BLE001
            since = (now - self._last_broker_ok).total_seconds() if self._last_broker_ok else float("inf")
            sev = "critical" if since >= th.get("broker_unreachable_seconds", 120) else "warning"
            rep.findings.append(Finding("broker_unreachable", f"{type(e).__name__}: {e}", sev))
            acct, open_orders = None, []
        if positions is not None:
            # 5. ledger vs broker positions
            ledger: dict[str, float] = {}
            for p in self.store.positions():
                ledger[p["symbol"]] = ledger.get(p["symbol"], 0.0) + float(p["qty"])
            for sym in set(ledger) | set(positions):
                b, l_ = (positions[sym].qty if sym in positions else 0.0), ledger.get(sym, 0.0)
                if abs(b - l_) > 1e-6:
                    rep.findings.append(Finding("position_mismatch", f"{sym}: broker {b:g} vs ledger {l_:g}", "warning"))
            # 6. daily P&L and drawdown vs policy, from the broker account (independent of the daemon)
            rs = self.store.load_risk() or {}
            day_start = float(rs.get("day_start_equity") or 0.0)
            peak = float(rs.get("peak_equity") or 0.0)
            if acct is not None and day_start > 0 and (day_start - acct.equity) / day_start >= th.get("daily_loss_pct", 0.03):
                rep.findings.append(Finding("daily_loss_breach", f"equity {acct.equity:,.2f} vs day start {day_start:,.2f}", "critical"))
            if acct is not None and peak > 0 and (peak - acct.equity) / peak >= th.get("drawdown_pct", 0.20):
                rep.findings.append(Finding("drawdown_breach", f"equity {acct.equity:,.2f} vs peak {peak:,.2f}", "critical"))
            # 7. orphaned orders: open at the broker, unknown to the store
            known = {o["client_order_id"] for o in self.store.orders()} | {o["broker_id"] for o in self.store.orders() if o.get("broker_id")}
            orphans = [o for o in open_orders if o.client_order_id not in known and o.id not in known]
            if orphans:
                rep.findings.append(Finding("orphaned_orders", ", ".join(f"{o.symbol} {o.side} {o.qty:g} {o.order_type}" for o in orphans), "warning"))
            # 8. unprotected overnight positions (after the close)
            if not regular:
                unp = [p for p in self.store.positions() if p.get("unprotected") or p.get("unprotected_overnight")]
                if unp:
                    rep.findings.append(Finding("unprotected_overnight", ", ".join(f"{p['symbol']}({p['module']})" for p in unp), "warning"))
        return rep

    def _tu_recent_events(self, now: datetime) -> bool:
        wm = _parse(self.store.watermark("trade_updates"))
        return bool(wm and (now - wm) < timedelta(hours=1))

    # --------------------------------------------------------------- actions
    def act(self, rep: WatchdogReport) -> list[str]:
        if rep.clean:
            if self.halt_flag.exists() and self._flag_is_ours():
                self.halt_flag.unlink()
                self.alert("watchdog: halt cleared", "all checks clean again", "info")
            return []
        halt_on, flatten_on = set(self.cfg.get("halt_on", [])), set(self.cfg.get("flatten_on", []))
        conds = {f.condition for f in rep.findings}
        if "heartbeat_stale_300s" in conds:
            conds.add("heartbeat_stale")
        summary = "; ".join(f"{f.condition}: {f.detail}" for f in rep.findings)
        done: list[str] = []
        if "alert" in self.enabled:
            level = "critical" if any(f.severity == "critical" for f in rep.findings) else "warning"
            self.alert("watchdog findings", summary, level)
            self._record("alert", summary)
            done.append("alert")
        if "halt_entries" in self.enabled and conds & halt_on:
            self.halt_flag.parent.mkdir(parents=True, exist_ok=True)
            self.halt_flag.write_text(f"watchdog {rep.ts.isoformat()}: {summary}\n", encoding="utf-8")
            self._record("halt_entries", summary)
            done.append("halt_entries")
        if "cancel_pending_entries" in self.enabled and conds & halt_on:
            n = 0
            for o in self.store.orders(open_only=True):
                if o["kind"] == "entry" and o.get("broker_id"):
                    try:
                        self.broker.cancel_order(o["broker_id"])
                        n += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("cancel %s failed: %s", o["client_order_id"], e)
            self._record("cancel_pending_entries", f"{n} entries cancelled")
            done.append("cancel_pending_entries")
        if "flatten" in self.enabled and conds & flatten_on and not (conds & flatten_on) <= self.flattened_for:
            try:
                self.broker.close_all_positions()
                self.flattened_for |= conds & flatten_on
                self._record("flatten", f"closed all positions on {sorted(conds & flatten_on)}")
                self.alert("watchdog: FLATTENED", summary, "critical")
                done.append("flatten")
            except Exception as e:  # noqa: BLE001
                self.alert("watchdog: flatten FAILED", f"{type(e).__name__}: {e}", "critical")
        rep.actions = done
        return done

    def _flag_is_ours(self) -> bool:
        try:
            return self.halt_flag.read_text(encoding="utf-8").startswith("watchdog ")
        except OSError:
            return False

    def _record(self, action: str, detail: str) -> None:
        self.store.heartbeat(f"watchdog:{action}", self.clock(), detail[:400])
        self.store.add_decision({"ts": self.clock().isoformat(), "decision": f"watchdog:{action}", "detail": detail}, kind="watchdog")

    # ------------------------------------------------------------------ loop
    def run_forever(self, interval: int | None = None) -> None:
        interval = interval or int(self.cfg.get("interval_seconds", 30))
        while not self._stop:
            try:
                rep = self.check()
                self.act(rep)
                self.store.heartbeat("watchdog", self.clock(), "clean" if rep.clean else f"{len(rep.findings)} findings")
            except Exception as e:  # noqa: BLE001
                log.exception("watchdog iteration failed: %s", e)
            for _ in range(interval):
                if self._stop:
                    break
                _time.sleep(1)

    def stop(self) -> None:
        self._stop = True
