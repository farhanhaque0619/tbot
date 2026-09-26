"""OrderManager: priority, idempotent trade updates, protective legs, fractional handling, flatten on lost protection."""
from datetime import date, datetime, timedelta

import pytest

from bot.backtest.simbroker import SimBroker
from bot.core.events import ScheduleEvent, TradeUpdateEvent
from bot.core.intents import TargetPosition
from bot.core.policy import RiskPolicy
from bot.data.calendar import NY
from bot.execution.oms import OrderManager
from bot.risk.policy_engine import RiskEngine
from tests.v15.helpers import bar, intent

D = date(2026, 9, 25)
T0 = datetime(2026, 9, 25, 10, 0, tzinfo=NY)
POL = RiskPolicy(name="t", allowed_modules=("A", "B"), max_gross_pct=1.5, max_symbol_exposure_pct=1.0, max_sector_pct_of_gross=1.0,
                 allow_short=True, allow_margin=True, max_open_positions=10, require_broker_protection_overnight=True)


class Harness:
    def __init__(self, cash=100_000.0, *, whole=True, policy=POL, software_stops=False):
        self.updates = []
        self.broker = SimBroker(cash, on_trade_update=self.updates.append)
        self.broker.now = T0
        self.broker.mark("SPY", 100.0, T0)
        self.broker.mark("QQQ", 50.0, T0)
        self.risk = RiskEngine(policy, allow_fractional=True)
        self.alerts = []
        self.oms = OrderManager(self.broker, self.risk, policy, run_id="t", clock=lambda: self.broker.now, whole_share_capable=whole,
                                on_alert=lambda t, m: self.alerts.append((t, m)), software_stops=software_stops)
        self.prices = {"SPY": 100.0, "QQQ": 50.0}

    def target(self, qty, symbol="SPY", module="A", style="market", exit_style="market", stop=None, overnight=True, kind="entry"):
        it = intent(symbol, module, direction=(1 if qty > 0 else -1) or 1, price=self.prices[symbol], style=style, exit_style=exit_style,
                    overnight=overnight, stop=stop)
        return TargetPosition(symbol, module, qty, abs(qty) * self.prices[symbol], self.prices[symbol], style, exit_style, stop, overnight, it, (), kind)

    def reconcile(self, targets, **kw):
        return self.oms.reconcile(targets, account=self.broker.get_account(), prices=self.prices, session=D, market_open=True, **kw)

    def drain(self):
        while self.updates:
            self.oms.on_trade_update(self.updates.pop(0), session=D)

    def next_bar(self, symbol="SPY", **kw):
        self.broker.now = self.broker.now + timedelta(minutes=1)
        kw.setdefault("o", self.prices[symbol])
        b = bar(symbol, ts=self.broker.now, **kw)
        self.broker.step(b)
        self.prices[symbol] = b.close
        self.drain()
        return b


def test_exits_are_planned_before_entries():
    h = Harness()
    ts = [h.target(10, "SPY", "A"), h.target(0, "QQQ", "B", kind="exit"), h.target(5, "QQQ", "A")]
    planned = h.oms.plan(ts)
    assert planned[0].is_flat and [t.symbol for t in planned[1:]] == ["SPY", "QQQ"]


def test_entry_fill_updates_ledger_and_duplicate_updates_are_ignored():
    h = Harness()
    recs = h.reconcile([h.target(10)])
    assert len(recs) == 1 and recs[0].status in ("accepted", "new") and recs[0].kind == "entry"
    assert recs[0].client_order_id.startswith("t-A-SPY-2026-09-25-1-entry")
    assert h.risk.ledger.inflight, "in-flight notional reserved between submit and fill"
    h.next_bar()
    sl = h.risk.ledger.slice("A", "SPY")
    assert sl.qty == pytest.approx(10) and not h.risk.ledger.inflight
    # replay the same fill event (reconnect duplicate): ledger unchanged
    ev = TradeUpdateEvent(recs[0].broker_id, recs[0].client_order_id, "fill", h.broker.now, "SPY", "buy", 10, 10, 100.02, "filled")
    h.oms.on_trade_update(ev, session=D)
    h.oms.on_trade_update(ev, session=D)
    assert h.risk.ledger.slice("A", "SPY").qty == pytest.approx(10)


def test_open_order_blocks_second_order_for_same_slice():
    h = Harness()
    h.reconcile([h.target(10)])
    recs = h.reconcile([h.target(20)])
    assert recs == [] and h.oms.decisions[-1]["decision"] == "skip"


def test_whole_share_overnight_entry_carries_oto_stop_and_exit_books_trade():
    h = Harness()
    recs = h.reconcile([h.target(10, stop=95.0)])
    assert any("OTO stop leg" in e for e in recs[0].events)
    h.next_bar()
    key = ("A", "SPY")
    assert h.oms.protective[key] == recs[0].client_order_id + "-stop"
    stop_orders = [o for o in h.broker.by_id.values() if o.order_type == "stop"]
    assert len(stop_orders) == 1 and stop_orders[0].stop_price == 95.0 and not stop_orders[0].held
    # exit at a profit
    h.prices["SPY"] = 110.0
    ex = h.reconcile([h.target(0, kind="exit")])
    assert ex[0].kind == "exit" and ex[0].qty == 10
    h.next_bar(c=110.0)
    assert h.risk.ledger.slice(*key).qty == pytest.approx(0)
    assert len(h.oms.trades) == 1 and h.oms.trades[0]["pnl"] > 0


def test_stop_election_across_gap_fills_at_open_and_clears_protection():
    h = Harness()
    h.reconcile([h.target(10, stop=95.0)])
    h.next_bar()
    key = ("A", "SPY")
    assert key in h.oms.protective
    # gap down through the stop: elected on this bar, filled at next open (gapped)
    h.next_bar(o=90.0, c=90.0, l=89.0, h=91.0)
    h.next_bar(o=89.0, c=89.5)
    assert h.risk.ledger.slice(*key).qty == pytest.approx(0)
    assert key not in h.oms.protective
    fills = [f for f in h.broker.fills_log if f.get("reason", "").startswith("stop")]
    assert fills and fills[-1]["price"] <= 89.0 * (1 + 1e-3) and h.oms.trades[-1]["exit_reason"] == "stop"


def test_partial_fill_then_fill_averages_price_once():
    h = Harness()
    recs = h.reconcile([h.target(30)])
    rec = recs[0]
    oid = rec.broker_id
    now = h.broker.now
    h.oms.on_trade_update(TradeUpdateEvent(oid, rec.client_order_id, "partial_fill", now, "SPY", "buy", 30, 10, 100.0, "partially_filled"), session=D)
    h.oms.on_trade_update(TradeUpdateEvent(oid, rec.client_order_id, "fill", now + timedelta(seconds=1), "SPY", "buy", 30, 30, 101.0, "filled"), session=D)
    assert rec.filled_qty == 30 and rec.avg_price == pytest.approx((10 * 100 + 20 * 101) / 30)
    assert h.risk.ledger.slice("A", "SPY").qty == pytest.approx(30)


def test_fractional_position_gets_day_stop_and_is_flagged_unprotected_overnight():
    h = Harness(whole=False)
    recs = h.reconcile([h.target(2.5, stop=95.0)])
    assert not any("OTO" in e for e in recs[0].events)
    h.next_bar()
    sl = h.risk.ledger.slice("A", "SPY")
    stop = h.oms.orders[h.oms.protective[("A", "SPY")]]
    assert sl.qty == pytest.approx(2.5) and stop.tif == "day" and sl.unprotected_overnight and not sl.unprotected
    # pre-open next session re-places the DAY stop and cancels the old one
    h.broker.end_of_day(h.broker.now)
    h.drain()
    nxt = datetime(2026, 9, 28, 9, 0, tzinfo=NY)
    h.broker.now = nxt
    h.oms.on_schedule(ScheduleEvent("pre_open", nxt, date(2026, 9, 28)), prices=h.prices)
    new_stop = h.oms.orders[h.oms.protective[("A", "SPY")]]
    assert new_stop.client_order_id != stop.client_order_id and new_stop.is_open and new_stop.stop_price == 95.0


def test_lost_protective_flags_slice_and_flattens_when_policy_requires():
    h = Harness()
    h.reconcile([h.target(10, stop=95.0)])
    h.next_bar()
    key = ("A", "SPY")
    stop_cid = h.oms.protective[key]
    stop_rec = h.oms.orders[stop_cid]
    h.oms.on_trade_update(TradeUpdateEvent(stop_rec.broker_id, stop_cid, "canceled", h.broker.now, "SPY", "sell", 10, 0, None, "canceled"), session=D)
    assert h.risk.ledger.slice(*key).unprotected and key in h.oms.pending_flatten and h.alerts
    h.oms.on_schedule(ScheduleEvent("bar_close", h.broker.now, D, "1m"), prices=h.prices)
    assert any(o.kind == "flatten" for o in h.oms.orders.values()) and not h.oms.pending_flatten


def test_protective_rejection_is_alerted_not_swallowed():
    h = Harness()
    h.reconcile([h.target(10, stop=95.0)])
    h.next_bar()

    def boom(*a, **k):
        raise RuntimeError("broker down")
    h.broker.submit_stop_order = boom
    rec = h.oms.place_protective("A", "SPY", 96.0)
    assert rec.status == "submit_failed" and h.risk.ledger.slice("A", "SPY").unprotected and ("A", "SPY") in h.oms.pending_flatten
    assert any(t == "protective order rejected" for t, _ in h.alerts)


def test_risk_rejection_creates_record_without_broker_order():
    pol = POL.model_copy(update={"allowed_symbols": ("SPY",), "max_open_positions": 1})
    h = Harness(policy=pol)
    recs = h.reconcile([h.target(1_000_000)])   # far beyond equity/buying power
    assert recs[0].status == "risk_rejected" and not h.broker.by_id and h.oms.decisions[-1]["decision"] == "blocked"


def test_marketable_limit_is_repriced_once_then_abandoned():
    h = Harness()
    h.broker.set_quote_spread("SPY", 2.0)
    recs = h.reconcile([h.target(10, style="marketable_limit")], spreads={"SPY": 2.0})
    rec = recs[0]
    assert rec.limit_price is not None and rec.abandon_at == T0 + timedelta(minutes=3)
    # price runs away so the limit never fills
    h.prices["SPY"] = 105.0
    for _ in range(2):
        h.broker.now += timedelta(minutes=1)
        h.broker.step(bar("SPY", ts=h.broker.now, o=105.0, c=105.5, l=104.9, h=105.6))
        h.oms.tick(prices=h.prices, spreads={"SPY": 2.0})
        h.drain()
    assert rec.repriced and rec.limit_price >= 105.0
    h.broker.now += timedelta(minutes=2)
    h.oms.tick(prices=h.prices, spreads={"SPY": 2.0})
    h.drain()
    assert not rec.is_open and any("abandoned" in e for e in rec.events)


def test_translate_style_rules():
    h = Harness()
    o = h.oms
    assert o._translate_style("opg", "buy", 10, 100.0, None, "SPY", T0)[:2] == ("opg", "opg")
    assert o._translate_style("opg", "buy", 2.5, 100.0, None, "SPY", T0)[:2] == ("market", "day"), "fractional cannot use auction orders"
    assert o._translate_style("cls", "sell", 2.5, 100.0, None, "SPY", T0)[0] == "market_1558"
    late = datetime(2026, 9, 25, 15, 52, tzinfo=NY)
    assert o._translate_style("cls", "sell", 10, 100.0, None, "SPY", late)[0] is None, "past the CLS cutoff"
    st, tif, typ, lp = o._translate_style("marketable_limit", "buy", 10, 100.0, {"SPY": 4.0}, "SPY", T0)
    assert (typ, tif) == ("limit", "day") and lp == pytest.approx(100.03)


def test_kill_switch_flatten_all_and_cancel_entries():
    h = Harness()
    h.reconcile([h.target(10, "SPY", "A"), h.target(20, "QQQ", "B")])
    h.next_bar("SPY"); h.next_bar("QQQ")
    h.reconcile([h.target(30, "SPY", "B", style="marketable_limit")], spreads={"SPY": 2.0})
    n = h.oms.cancel_non_protective()
    assert n == 1
    out = h.oms.flatten_all(reason="kill switch")
    assert {o.symbol for o in out} == {"SPY", "QQQ"} and all(o.kind == "flatten" for o in out)
    h.next_bar("SPY"); h.next_bar("QQQ")
    assert h.risk.ledger.open_positions() == 0
