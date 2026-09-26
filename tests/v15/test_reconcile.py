"""Reconciliation: orphan adopt/flatten, unknown orders, protective adoption, fill replay, streak halts entries."""
from datetime import date, datetime

from bot.core.policy import RiskPolicy
from bot.data.calendar import NY
from bot.execution.fake_broker import FakeBroker
from bot.execution.oms import OrderManager
from bot.execution.reconcile import ORPHAN, reconcile
from bot.execution.store import ExecutionStore
from bot.risk.policy_engine import RiskEngine

D = date(2026, 9, 22)
T0 = datetime(2026, 9, 22, 20, 0, tzinfo=NY)
POL = RiskPolicy(name="t", allowed_modules=("A",), max_gross_pct=1.5, max_symbol_exposure_pct=1.0, max_sector_pct_of_gross=1.0, allow_margin=True,
                 allow_overnight={"A": True}, require_broker_protection_overnight=True)


def setup(tmp_path, policy=POL):
    broker = FakeBroker(prices={"SPY": 100.0, "QQQ": 50.0})
    broker.now = T0
    st = ExecutionStore(tmp_path / "x.sqlite")
    risk = RiskEngine(policy)
    alerts = []
    oms = OrderManager(broker, risk, policy, run_id="r", clock=lambda: broker.now, whole_share_capable=True, store=st, on_alert=lambda t, m: alerts.append(t))
    return broker, st, risk, oms, alerts


def test_unknown_broker_position_is_adopted_with_protection(tmp_path):
    broker, st, risk, oms, alerts = setup(tmp_path)
    broker.positions["QQQ"], broker.avg_price["QQQ"] = 4.0, 50.0
    rep = reconcile(broker=broker, oms=oms, policy=POL, prices={"QQQ": 50.0}, session=D, now=T0, store=st, alert=lambda t, m: alerts.append(t))
    assert rep.adopted_positions == ["QQQ"] and risk.ledger.slice(ORPHAN, "QQQ").qty == 4.0
    assert (ORPHAN, "QQQ") in oms.protective and broker.orders[oms.protective[(ORPHAN, "QQQ")]].stop_price == 47.5
    assert any(d["kind"] == "position_mismatch" for d in rep.discrepancies) and "reconciliation: adopted orphan position" in alerts
    assert st.positions()[0]["module"] == ORPHAN and st.decisions(kind="reconciliation")
    assert not rep.halted_entries and st.meta("reconcile_unresolved_streak") == "1"


def test_flatten_policy_and_two_consecutive_discrepancies_halt_entries(tmp_path):
    pol = POL.model_copy(update={"orphan_policy": "flatten"})
    broker, st, risk, oms, alerts = setup(tmp_path, pol)
    broker.positions["QQQ"], broker.avg_price["QQQ"] = 4.0, 50.0
    rep = reconcile(broker=broker, oms=oms, policy=pol, prices={"QQQ": 50.0}, session=D, now=T0, store=st)
    assert rep.flattened == ["QQQ"] and any(o.kind == "flatten" and o.module_id == ORPHAN for o in oms.orders.values())
    assert not risk.halt_entries
    broker.positions["SPY"] = 2.0          # still not flat and another unknown position: second consecutive discrepancy
    rep2 = reconcile(broker=broker, oms=oms, policy=pol, prices={"QQQ": 50.0, "SPY": 100.0}, session=D, now=T0, store=st)
    assert rep2.halted_entries and risk.halt_entries and "consecutive" in risk.halt_reason


def test_unknown_orders_are_cancelled_or_adopted_and_submitting_rows_resolve(tmp_path):
    broker, st, risk, oms, alerts = setup(tmp_path)
    broker.submit_limit_order("SPY", 3, "buy", 99.0, "manual-1", "day")
    broker.positions["SPY"], broker.avg_price["SPY"] = 10.0, 100.0
    risk.ledger.apply_fill("A", "SPY", 10.0, 100.0, D)          # ledger agrees with the broker on SPY
    broker.submit_stop_order("SPY", 10, "sell", 95.0, "manual-stop", "gtc")
    rep = reconcile(broker=broker, oms=oms, policy=POL, prices={"SPY": 100.0}, session=D, now=T0, store=st)
    assert rep.canceled_orders == ["manual-1"] and broker.orders["manual-1"].status == "canceled"
    assert rep.adopted_orders == ["manual-stop"] and oms.protective[("A", "SPY")] == "manual-stop" and oms.orders["manual-stop"].kind == "stop"
    assert not [d for d in rep.discrepancies if d["kind"] == "position_mismatch"]


def test_fills_since_watermark_are_replayed_once(tmp_path):
    from bot.core.intents import TargetPosition
    from tests.v15.helpers import intent
    broker, st, risk, oms, alerts = setup(tmp_path)
    st.set_watermark("orders_since", T0.replace(hour=1))
    it = intent("SPY", "A", overnight=True)
    rec = oms.reconcile([TargetPosition("SPY", "A", 5, 500.0, 100.0, "market", "market", None, True, it, (), "entry")],
                        account=broker.get_account(), prices={"SPY": 100.0}, session=D, market_open=True)[0]
    broker.fill(rec.client_order_id)                        # filled at the broker while we were down: no trade update seen
    assert risk.ledger.slice("A", "SPY").qty == 0
    rep = reconcile(broker=broker, oms=oms, policy=POL, prices={"SPY": 100.0}, session=D, now=T0, store=st)
    assert rep.replayed_fills == 1 and risk.ledger.slice("A", "SPY").qty == 5
    rep2 = reconcile(broker=broker, oms=oms, policy=POL, prices={"SPY": 100.0}, session=D, now=T0.replace(minute=1), store=st)
    assert rep2.replayed_fills == 0 and rep2.clean and risk.ledger.slice("A", "SPY").qty == 5


def test_ledger_position_missing_at_broker_is_cleared(tmp_path):
    broker, st, risk, oms, alerts = setup(tmp_path)
    risk.ledger.apply_fill("A", "SPY", 10.0, 100.0, D)
    rep = reconcile(broker=broker, oms=oms, policy=POL, prices={"SPY": 100.0}, session=D, now=T0, store=st)
    assert risk.ledger.slice("A", "SPY").qty == 0 and any(d["kind"] == "position_missing_at_broker" for d in rep.discrepancies)
