"""ExecutionStore: WAL, two-transaction submit, idempotent fills, slices round trip, migration from the V1 JSON state,
and the OrderManager running with persistence across a restart."""
import json
from datetime import date, datetime


from bot.core.events import TradeUpdateEvent
from bot.core.intents import TargetPosition
from bot.core.policy import RiskPolicy
from bot.data.calendar import NY
from bot.execution.fake_broker import FakeBroker
from bot.execution.oms import OrderManager
from bot.execution.state import BotState, StateStore
from bot.execution.store import ExecutionStore
from bot.risk.policy_engine import RiskEngine
from tests.v15.helpers import intent

D = date(2026, 9, 22)
T0 = datetime(2026, 9, 22, 20, 0, tzinfo=NY)
POL = RiskPolicy(name="t", allowed_modules=("A",), max_gross_pct=1.5, max_symbol_exposure_pct=1.0, max_sector_pct_of_gross=1.0, allow_margin=True,
                 allow_overnight={"A": True}, require_broker_protection_overnight=True)


def target(qty, price=100.0, stop=None):
    it = intent("SPY", "A", price=price, stop=stop, overnight=True)
    return TargetPosition("SPY", "A", qty, abs(qty) * price, price, "market", "market", stop, True, it, (), "entry" if qty else "exit")


def make(store, broker=None):
    broker = broker or FakeBroker(prices={"SPY": 100.0})
    broker.now = T0
    risk = RiskEngine(POL)
    oms = OrderManager(broker, risk, POL, run_id="r", clock=lambda: broker.now, whole_share_capable=True, store=store)
    return broker, risk, oms


def test_store_is_wal_and_orders_go_submitting_then_updated(tmp_path):
    st = ExecutionStore(tmp_path / "x.sqlite")
    assert st.con.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and st.meta("schema_version") == "1"
    broker, risk, oms = make(st)
    recs = oms.reconcile([target(10, stop=95.0)], account=broker.get_account(), prices={"SPY": 100.0}, session=D, market_open=True)
    rows = st.orders()
    assert len(rows) == 1 and rows[0]["status"] == recs[0].status and rows[0]["broker_id"] == recs[0].broker_id and rows[0]["module"] == "A"
    assert st.decisions()[0]["decision"] == "submit"
    # a crash between begin_submit and the broker's answer leaves a 'submitting' row
    st.begin_submit(type("R", (), {**recs[0].__dict__, "client_order_id": "r-A-SPY-2026-09-22-9-entry", "broker_id": None, "status": "submitting"})())
    assert [r["client_order_id"] for r in st.submitting_orders()] == ["r-A-SPY-2026-09-22-9-entry"]


def test_fills_are_idempotent_across_restart_and_slices_round_trip(tmp_path):
    st = ExecutionStore(tmp_path / "x.sqlite")
    broker, risk, oms = make(st)
    rec = oms.reconcile([target(10, stop=95.0)], account=broker.get_account(), prices={"SPY": 100.0}, session=D, market_open=True)[0]
    ev = TradeUpdateEvent(rec.broker_id, rec.client_order_id, "fill", T0, "SPY", "buy", 10, 10, 100.5, "filled")
    oms.on_trade_update(ev, session=D)
    assert risk.ledger.slice("A", "SPY").qty == 10 and st.positions()[0]["qty"] == 10 and st.orders()[0]["filled_qty"] == 10
    assert st.record_fill(ev) is False, "same (order_id, event, ts) is rejected by the store"
    # restart: a fresh OMS restores orders, slices, protective map and refuses the replayed fill
    broker2 = broker
    risk2 = RiskEngine(POL)
    oms2 = OrderManager(broker2, risk2, POL, run_id="r", clock=lambda: broker2.now, whole_share_capable=True, store=st)
    n = oms2.restore()
    assert n >= 1 and risk2.ledger.slice("A", "SPY").qty == 10 and rec.client_order_id in oms2.orders
    oms2.on_trade_update(ev, session=D)
    assert risk2.ledger.slice("A", "SPY").qty == 10, "replayed fill after restart did not double count"
    assert oms2._seq[("A", "SPY", D)] >= 1, "sequence continues after the restored orders"
    # protective stop placement is persisted
    prot = oms2.place_protective("A", "SPY", 96.0)
    assert st.protectives()[("A", "SPY")]["client_order_id"] == prot.client_order_id and st.protectives()[("A", "SPY")]["stop_price"] == 96.0


def test_risk_heartbeats_throttles_watermarks_trades(tmp_path):
    st = ExecutionStore(tmp_path / "x.sqlite")
    from bot.risk.manager import RiskState
    st.save_risk(RiskState(peak_equity=100.0, killed=True, kill_reason="dd"))
    assert st.load_risk()["killed"] is True and st.load_risk()["kill_reason"] == "dd"
    st.heartbeat("daemon", T0, "ok"); st.heartbeat("daemon", T0.replace(hour=21), "ok2")
    assert st.heartbeats()["daemon"][1] == "ok2" and len(st.heartbeat_log("daemon")) == 2
    st.set_throttle("M2", 0.5, "losing streak")
    assert st.throttles() == {"M2": (0.5, "losing streak")} and st.clear_throttle("M2") and not st.throttles() and not st.clear_throttle("M2")
    st.set_watermark("orders_since", T0)
    assert st.watermark("orders_since") == T0.isoformat() and st.watermark("nope") is None
    st.add_trade({"module": "M2", "symbol": "SPY", "qty": 1, "entry_price": 1, "exit_price": 2, "pnl": 1.0, "exit_reason": "exit", "exit_ts": T0, "session": D})
    assert st.trades("M2")[0]["pnl"] == 1.0 and st.trades("M1") == []


def test_migrate_from_v1_json_state(tmp_path):
    js = tmp_path / "paper.json"
    state = BotState(run_id="paper", env="paper", strategy="ma_crossover", symbols=["SPY"],
                     orders={"paper-SPY-2026-09-20-entry": {"id": "b1", "symbol": "SPY", "side": "buy", "qty": 3, "status": "filled", "filled_qty": 3, "filled_avg_price": 99.0,
                                                            "reason": "entry", "bar_date": "2026-09-19", "tif": "opg"}},
                     positions={"SPY": {"qty": 3, "side": 1, "entry_price": 99.0, "entry_date": "2026-09-20", "stop": 95.0}},
                     risk={"peak_equity": 101.0, "killed": False}, trades=[{"symbol": "SPY", "qty": 1, "entry_price": 90, "exit_price": 95, "pnl": 5.0, "exit_reason": "signal"}])
    StateStore(js).save(state)
    st = ExecutionStore(tmp_path / "x.sqlite")
    n = st.migrate_from_json(js)
    assert n == {"orders": 1, "positions": 1, "trades": 1}
    pos = st.positions()[0]
    assert pos["module"] == "ma_crossover" and pos["qty"] == 3 and pos["unprotected"] == 1, "migrated positions are flagged unprotected until reconciled"
    assert st.orders()[0]["status"] == "filled" and st.load_risk()["peak_equity"] == 101.0 and st.meta("migrated_from") == str(js)
    assert st.migrate_from_json(js) == {"orders": 0, "positions": 0, "trades": 1}, "idempotent for orders and positions"
    # the JSON reader still works (one release)
    assert json.loads(js.read_text())["run_id"] == "paper" and StateStore(js).load().positions["SPY"]["qty"] == 3
