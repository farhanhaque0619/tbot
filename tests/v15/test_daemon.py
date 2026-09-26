"""Daemon lifecycle offline: refusals, boot, cycles, halt flag, shutdown, restart safety."""
from datetime import date, datetime, time, timedelta

import pytest

from bot.config import LIVE_CONFIRMATION_PHRASE, Settings
from bot.core.bus import EventBus
from bot.core.events import BarEvent, TradeUpdateEvent
from bot.core.policy import RiskPolicy
from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from bot.execution.fake_broker import FakeBroker
from bot.execution.store import ExecutionStore
from bot.runtime.daemon import Daemon, DaemonRefused
from tests.v15.helpers import intent

CAL = SessionCalendar()
D = date(2026, 9, 22)


class Universe:
    tier1, tier2, tier3, sectors = ["SPY", "QQQ"], [], [], {}
    symbols = ["SPY", "QQQ"]


class Rec:
    def __init__(self):
        self.msgs = []

    def send(self, title, message="", *, level="info"):
        self.msgs.append((title, message, level))
        return True


class Script:
    module_id, symbols, listens = "A", ["SPY"], {"bar_close_1m", "session_close"}

    def __init__(self):
        self.calls, self.emit = [], []

    def on_event(self, ev, snap, pos):
        self.calls.append((ev.kind, ev.ts, pos.qty))
        out, self.emit = self.emit, []
        return out


def settings(tmp_path, **kw):
    base = dict(alpaca_paper_api_key="PKTESTTESTTESTTEST", alpaca_paper_secret_key="x" * 40, trading_env="paper", state_dir=tmp_path)
    base.update(kw)
    return Settings(**base)


def policy_file(tmp_path, **over):
    base = dict(name="t", allowed_modules=("A",), allow_overnight={"A": True}, max_gross_pct=1.5, max_symbol_exposure_pct=1.0,
                max_sector_pct_of_gross=1.0, allow_margin=True, require_broker_protection_overnight=True)
    base.update(over)
    pol = RiskPolicy(**base)
    return pol.save(tmp_path / "policy.yaml"), pol


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, *, now=None, broker=None, store=None, module=None, s=None, whole=True):
    s = s or settings(tmp_path)
    p, pol = policy_file(tmp_path)
    broker = broker or FakeBroker(prices={"SPY": 100.0, "QQQ": 50.0})
    clock = Clock(now or datetime.combine(D, time(10, 0), NY))
    broker.now = clock.t
    store = store or ExecutionStore(tmp_path / "paper.sqlite")
    mod = module or Script()
    d = Daemon(s, env="paper", policy_path=p, modules=[mod], broker=broker, store=store, calendar=CAL, alerter=Rec(), clock=clock, universe=Universe(),
               bus=EventBus(), whole_share_capable=whole, halt_flag_path=tmp_path / "paper.halt")
    return d, mod, broker, store, clock


def bar(sym, t, c=100.0):
    return BarEvent(sym, t, c, c + 0.1, c - 0.1, c, 1000.0, "1m", t.date())


def test_live_refusals_before_anything_starts(tmp_path):
    p, _ = policy_file(tmp_path)
    s = settings(tmp_path)
    with pytest.raises(ValueError, match="unpromoted"):
        Daemon(s, env="live", policy_path=p, cli_live_flag=True).boot()          # module A has no promotion record
    prom = tmp_path / "PROMOTIONS.md"
    prom.write_text("## A promoted 2026-01-01 test\n")
    import bot.core.policy as cp
    cp_path = cp.PROMOTIONS_PATH
    cp.PROMOTIONS_PATH = prom
    try:
        with pytest.raises(DaemonRefused, match="TRADING_ENV"):
            Daemon(s, env="live", policy_path=p, cli_live_flag=True).boot()
        s2 = settings(tmp_path, trading_env="live", alpaca_live_api_key="AKTESTTESTTESTTEST", alpaca_live_secret_key="y" * 40)
        with pytest.raises(DaemonRefused, match="--live"):
            Daemon(s2, env="live", policy_path=p, cli_live_flag=False).boot()
        with pytest.raises(DaemonRefused, match="LIVE_AUTONOMOUS_TRADING"):
            Daemon(s2, env="live", policy_path=p, cli_live_flag=True).boot()
        s3 = settings(tmp_path, trading_env="live", alpaca_live_api_key="AKTESTTESTTESTTEST", alpaca_live_secret_key="y" * 40, live_autonomous_trading=True)
        with pytest.raises(DaemonRefused, match="interlock"):
            Daemon(s3, env="live", policy_path=p, cli_live_flag=True).boot()
        from bot.execution.interlock import LiveInterlock
        il = LiveInterlock(s3)
        il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True, policy_fingerprint="0" * 64)
        with pytest.raises(DaemonRefused, match="policy changed"):
            Daemon(s3, env="live", policy_path=p, cli_live_flag=True, interlock=il).boot()
    finally:
        cp.PROMOTIONS_PATH = cp_path


def test_boot_records_policy_and_cycle_dispatches_orders_and_fills(tmp_path):
    d, mod, broker, store, clock = make(tmp_path)
    d.boot()
    assert store.meta("policy_fingerprint") == d.policy.fingerprint() and store.heartbeats()["daemon"][1] == "boot"
    assert any(t == "[PAPER] daemon started" for t, _, _ in d.alerter.msgs)
    # a bar arrives; the module wants SPY
    mod.emit = [intent("SPY", "A", risk=0.001, vol=0.01, overnight=True, stop=95.0, ts=clock.t)]
    d.bus.publish(bar("SPY", clock.t))
    n = d.cycle(wait=0)
    assert n == 1 and mod.calls[-1][0] == "bar_close" and len(broker.submitted) >= 1
    rec = next(o for o in d.oms.orders.values() if o.kind == "entry")
    assert rec.status in ("new", "accepted") and store.orders()[0]["client_order_id"] == rec.client_order_id
    assert any(t.startswith("[PAPER] entry: SPY A") for t, _, _ in d.alerter.msgs)
    # fill via trade update
    broker.fill(rec.client_order_id, price=100.05)
    d.bus.publish(TradeUpdateEvent(rec.broker_id, rec.client_order_id, "fill", clock.t, "SPY", "buy", rec.qty, rec.qty, 100.05, "filled"))
    d.cycle(wait=0)
    assert d.risk.ledger.slice("A", "SPY").qty == rec.qty and store.positions()[0]["qty"] == rec.qty
    assert any(t.startswith("[PAPER] fill: SPY A") and "slippage" in m for t, m, _ in d.alerter.msgs)
    assert ("A", "SPY") in d.oms.protective, "whole-share overnight entry carries a stop leg"
    # policy fingerprint change on the next paper boot is recorded, not refused
    p2, _ = policy_file(tmp_path, max_gross_pct=1.2)
    d2 = Daemon(d.settings, env="paper", policy_path=p2, modules=[Script()], broker=broker, store=store, calendar=CAL, alerter=Rec(), clock=clock,
                universe=Universe(), whole_share_capable=True, halt_flag_path=tmp_path / "paper.halt")
    d2.boot()
    assert any(x.get("decision") == "policy_changed" for x in store.decisions(kind="operator"))


def test_watchdog_halt_flag_blocks_entries_and_clears(tmp_path):
    d, mod, broker, store, clock = make(tmp_path)
    d.boot()
    d.halt_flag.write_text("watchdog 2026-09-22: heartbeat_stale")
    mod.emit = [intent("SPY", "A", risk=0.001, overnight=True, ts=clock.t)]
    d.bus.publish(bar("SPY", clock.t)); d.cycle(wait=0)
    assert d.risk.halt_entries and not broker.submitted and d.oms.decisions[-1]["decision"] == "not_admitted"
    assert any("halted by the watchdog" in t for t, _, _ in d.alerter.msgs)
    d.halt_flag.unlink()
    mod.emit = [intent("SPY", "A", risk=0.001, overnight=True, ts=clock.t)]
    d.bus.publish(bar("SPY", clock.t + timedelta(minutes=1))); d.cycle(wait=0)
    assert not d.risk.halt_entries and broker.submitted


def test_schedule_events_fire_and_daily_summary(tmp_path):
    d, mod, broker, store, clock = make(tmp_path, now=datetime.combine(D, time(15, 59), NY))
    d.boot()
    clock.t = datetime.combine(D, time(16, 31), NY)
    d.cycle(wait=0)
    assert [c[0] for c in mod.calls] == ["session_close"]
    assert any(t == "[PAPER] daily summary" for t, _, _ in d.alerter.msgs)
    assert store.heartbeat_log("daemon_session")


def test_graceful_shutdown_cancels_entries_keeps_protection_and_exits_zero(tmp_path):
    d, mod, broker, store, clock = make(tmp_path)
    d.boot()
    mod.emit = [intent("SPY", "A", risk=0.001, overnight=True, stop=95.0, ts=clock.t, style="marketable_limit")]
    d.bus.publish(bar("SPY", clock.t)); d.cycle(wait=0)
    entry = next(o for o in d.oms.orders.values() if o.kind == "entry")
    d.risk.ledger.apply_fill("A", "QQQ", 5, 50.0, D)
    d.oms.place_protective("A", "QQQ", 45.0)
    stop_cid = d.oms.protective[("A", "QQQ")]
    d._on_signal()
    code = d.run(max_cycles=1)
    assert code == 0 and broker.orders[entry.client_order_id].status == "canceled" and broker.orders[stop_cid].is_open
    assert store.heartbeats()["daemon"][1] == "shutdown" and store.positions() and any(t == "[PAPER] daemon stopped" for t, _, _ in d.alerter.msgs)


def test_restart_reattaches_protection_and_resolves_pending_without_duplicate(tmp_path):
    d, mod, broker, store, clock = make(tmp_path)
    d.boot()
    # open position persisted without protection (e.g. protective was lost) and an order row committed but never answered
    d.risk.ledger.apply_fill("A", "SPY", 10, 100.0, D)
    broker.positions["SPY"], broker.avg_price["SPY"] = 10.0, 100.0
    store.save_slices(d.risk.ledger)
    from bot.execution.oms import OrderRecord
    pending = OrderRecord("paper-A-SPY-2026-09-22-7-entry", "A", "SPY", "buy", 3, "entry", "market", D, clock.t, 100.0, status="submitting")
    store.begin_submit(pending)
    broker.submit_market_order("SPY", 3, "buy", pending.client_order_id, "day")     # the broker DID get it before the crash
    lost = OrderRecord("paper-A-QQQ-2026-09-22-1-entry", "A", "QQQ", "buy", 2, "entry", "market", D, clock.t, 50.0, status="submitting")
    store.begin_submit(lost)                                                            # this one never reached the broker
    n_before = len(broker.submitted)
    d2 = Daemon(d.settings, env="paper", policy_path=d.policy_path, modules=[Script()], broker=broker, store=store, calendar=CAL, alerter=Rec(), clock=clock,
                universe=Universe(), whole_share_capable=True, halt_flag_path=tmp_path / "paper.halt")
    d2.boot()
    assert ("A", "SPY") in d2.oms.protective and any("protection re-attached" in t for t, _, _ in d2.alerter.msgs)
    assert d2.oms.orders[pending.client_order_id].broker_id is not None and d2.oms.orders[pending.client_order_id].is_open
    assert d2.oms.orders[lost.client_order_id].status == "submit_failed" and store.orders(status="submit_failed")
    assert len(broker.submitted) == n_before + 1, "only the protective stop was submitted; nothing re-sent"
    # the module asks for SPY again: the open pending order blocks a duplicate
    d2.modules[0].emit = [intent("SPY", "A", risk=0.001, overnight=True, ts=clock.t)]
    d2.bus.publish(bar("SPY", clock.t)); d2.cycle(wait=0)
    assert d2.oms.decisions[-1]["decision"] == "skip" and len(broker.submitted) == n_before + 1


def test_kill_switch_cancels_and_flattens_once(tmp_path):
    d, mod, broker, store, clock = make(tmp_path)
    d.boot()
    d.risk.ledger.apply_fill("A", "SPY", 10, 100.0, D)
    broker.positions["SPY"], broker.avg_price["SPY"] = 10.0, 100.0
    broker.cash = 70_000.0                     # equity collapses from the 100k peak recorded at boot
    broker.prices["SPY"] = 100.0
    d.bus.publish(bar("SPY", clock.t)); d.cycle(wait=0)
    assert d.risk.killed and any(o.kind == "flatten" for o in d.oms.orders.values()) and any("KILL SWITCH" in t for t, _, _ in d.alerter.msgs)
    n = len(broker.submitted)
    d.bus.publish(bar("SPY", clock.t + timedelta(minutes=1))); d.cycle(wait=0)
    assert len(broker.submitted) == n, "flatten happens once"
