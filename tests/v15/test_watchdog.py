from datetime import date, datetime, time, timedelta

from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from bot.execution.fake_broker import FakeAPIError, FakeBroker
from bot.execution.oms import OrderRecord
from bot.execution.store import ExecutionStore
from bot.runtime.watchdog import Watchdog, load_config

CAL = SessionCalendar()
D = date(2026, 9, 22)
T = datetime.combine(D, time(10, 0), NY)


def make(tmp_path, **cfg_over):
    cfg = load_config("config/watchdog.yaml")
    cfg.update(cfg_over)
    broker = FakeBroker(prices={"SPY": 100.0})
    broker.now = T
    st = ExecutionStore(tmp_path / "paper.sqlite")
    alerts = []
    wd = Watchdog(cfg, broker=broker, store=st, calendar=CAL, alert=lambda t, m, level="warning": alerts.append((t, level)), clock=lambda: T,
                  halt_flag_path=tmp_path / "paper.halt")
    return wd, broker, st, alerts


def test_config_loads_with_defaults_and_escalation_list():
    cfg = load_config("config/watchdog.yaml")
    assert cfg["actions"] == ["alert", "halt_entries", "cancel_pending_entries", "flatten"] and "drawdown_breach" in cfg["flatten_on"]
    assert load_config("does/not/exist.yaml")["actions"] == ["alert"]


def test_clean_state_has_no_findings_and_clears_our_flag(tmp_path):
    wd, broker, st, alerts = make(tmp_path)
    st.heartbeat("daemon", T - timedelta(seconds=10), "ok"); st.set_meta("last_bar_ts", (T - timedelta(seconds=30)).isoformat()); st.heartbeat("trade_updates", T, "connected")
    wd.halt_flag.write_text("watchdog earlier: heartbeat_stale")
    rep = wd.check()
    assert rep.clean, rep.findings
    assert wd.act(rep) == [] and not wd.halt_flag.exists() and ("watchdog: halt cleared", "info") in alerts


def test_stale_heartbeat_escalates_halt_and_cancels_entries_but_never_protective_legs(tmp_path):
    wd, broker, st, alerts = make(tmp_path)
    st.heartbeat("daemon", T - timedelta(seconds=400), "ok"); st.set_meta("last_bar_ts", T.isoformat()); st.heartbeat("trade_updates", T, "connected")
    e = broker.submit_limit_order("SPY", 3, "buy", 90.0, "c-entry", "day")
    s = broker.submit_stop_order("SPY", 3, "sell", 80.0, "c-stop", "gtc")
    st.update_order(OrderRecord("c-entry", "A", "SPY", "buy", 3, "entry", "marketable_limit", D, T, 100.0, broker_id=e.id, status="new"))
    st.update_order(OrderRecord("c-stop", "A", "SPY", "sell", 3, "stop", "protective", D, T, 80.0, broker_id=s.id, status="new"))
    rep = wd.check()
    assert [f.condition for f in rep.findings] == ["heartbeat_stale_300s"] and rep.findings[0].severity == "critical"
    acts = wd.act(rep)
    assert acts == ["alert", "halt_entries", "cancel_pending_entries", "flatten"]
    assert wd.halt_flag.read_text().startswith("watchdog ") and broker.orders["c-entry"].status == "canceled"
    assert "close_all_positions" in broker.calls and ("watchdog: FLATTENED", "critical") in alerts
    hb = st.heartbeats()
    assert all(f"watchdog:{a}" in hb for a in acts) and st.decisions(kind="watchdog")
    # a second identical report does not flatten again
    assert "flatten" not in wd.act(wd.check())
    # protective leg untouched by cancel_pending_entries path (only flatten closes everything)
    wd2, broker2, st2, _ = make(tmp_path / "b", actions=["alert", "halt_entries", "cancel_pending_entries"])
    st2.heartbeat("daemon", T - timedelta(seconds=400), "ok"); st2.set_meta("last_bar_ts", T.isoformat()); st2.heartbeat("trade_updates", T, "connected")
    s2 = broker2.submit_stop_order("SPY", 3, "sell", 80.0, "c-stop", "gtc")
    st2.update_order(OrderRecord("c-stop", "A", "SPY", "sell", 3, "stop", "protective", D, T, 80.0, broker_id=s2.id, status="new"))
    wd2.act(wd2.check())
    assert broker2.orders["c-stop"].is_open and "close_all_positions" not in broker2.calls


def test_position_mismatch_orphans_loss_and_drawdown_and_broker_unreachable(tmp_path):
    wd, broker, st, alerts = make(tmp_path, flatten_on=["drawdown_breach"])
    st.heartbeat("daemon", T, "ok"); st.set_meta("last_bar_ts", T.isoformat()); st.heartbeat("trade_updates", T, "connected")
    broker.positions["SPY"], broker.avg_price["SPY"] = 4.0, 100.0
    broker.submit_limit_order("SPY", 1, "buy", 90.0, "manual", "day")
    st.save_risk({"peak_equity": 130_000.0, "day_start_equity": 105_000.0})
    rep = wd.check()
    conds = {f.condition: f for f in rep.findings}
    assert {"position_mismatch", "orphaned_orders", "daily_loss_breach", "drawdown_breach"} <= set(conds)
    assert conds["drawdown_breach"].severity == "critical"
    acts = wd.act(rep)
    assert "flatten" in acts and "halt_entries" in acts
    broker.fail_next(FakeAPIError("boom", 503), times=5)
    rep2 = wd.check()
    assert any(f.condition == "broker_unreachable" for f in rep2.findings)


def test_feed_stale_only_in_regular_hours_and_unprotected_after_close(tmp_path):
    wd, broker, st, alerts = make(tmp_path)
    st.heartbeat("daemon", T, "ok"); st.heartbeat("trade_updates", T, "connected")
    rep = wd.check()
    assert [f.condition for f in rep.findings] == ["feed_stale"]
    wd.clock = lambda: datetime.combine(D, time(18, 0), NY)
    st.heartbeat("daemon", wd.clock(), "ok")
    from bot.risk.policy_engine import ExposureLedger
    led = ExposureLedger()
    led.apply_fill("A", "SPY", 1.5, 100.0, D)
    led.slice("A", "SPY").unprotected_overnight = True
    st.save_slices(led)
    broker.positions["SPY"], broker.avg_price["SPY"] = 1.5, 100.0
    rep = wd.check()
    assert [f.condition for f in rep.findings] == ["unprotected_overnight"]
    assert wd.act(rep) == ["alert"], "unprotected_overnight only alerts"
