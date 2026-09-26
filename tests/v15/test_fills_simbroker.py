from datetime import datetime, timedelta

import pytest

from bot.backtest.fills import FillEngine, FillParams
from bot.backtest.simbroker import SimBroker
from bot.data.calendar import NY
from bot.execution.fake_broker import DuplicateClientOrderId
from tests.v15.helpers import bar

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=NY)


def test_marketable_limit_and_market_prices():
    fe = FillEngine(FillParams(slippage_bps=2, default_spread_bps_etf=1.0, market_extra_bps=1.0))
    nb = bar(o=100.0)
    f = fe.marketable_limit(+1, 10, nb, symbol_kind="etf", quoted_spread_bps=None)
    assert f.price == pytest.approx(100.0 * (1 + 2.5 / 1e4)) and f.remaining == 0
    m = fe.market(-1, 10, nb, symbol_kind="stock", quoted_spread_bps=4.0)
    assert m.price == pytest.approx(100.0 * (1 - (2 + 2 + 1) / 1e4))
    assert fe.marketable_limit(+1, 10, nb, symbol_kind="etf", quoted_spread_bps=None, limit_price=100.01) is None, "limit respected"


def test_auction_fills():
    fe = FillEngine(FillParams(open_auction_slippage_bps=3, close_auction_slippage_bps=1))
    assert fe.auction_open(+1, 5, 200.0).price == pytest.approx(200.0 * 1.0003)
    assert fe.auction_close(-1, 5, 200.0).price == pytest.approx(200.0 * 0.9999)


def test_stop_election_and_gap():
    fe = FillEngine(FillParams(stop_slippage_bps=5))
    assert fe.stop_elected(-1, 99.0, bar(o=100, l=98.9, c=99.5)) and not fe.stop_elected(-1, 99.0, bar(o=100, l=99.1))
    normal = fe.stop_fill(-1, 10, 99.0, bar(o=99.3), gapped=False)
    assert normal.price == pytest.approx(99.0 * (1 - 5 / 1e4))
    gap = fe.stop_fill(-1, 10, 99.0, bar(o=95.0), gapped=True)
    assert gap.price == pytest.approx(95.0 * (1 - 5 / 1e4)), "gap through the stop fills at the open, not the stop"


def test_partial_fill_participation_cap():
    fe = FillEngine(FillParams(participation_cap=0.01))
    nb = bar(o=100.0, v=1_000.0)   # $100k bar -> 1% = $1,000 -> 10 shares
    f = fe.marketable_limit(+1, 25, nb, symbol_kind="etf", quoted_spread_bps=None)
    assert f.qty == pytest.approx(10, rel=1e-3) and f.remaining == pytest.approx(15, rel=1e-3)


def test_stress_hooks_are_deterministic_under_seed():
    a = FillEngine(FillParams(adverse_fill_prob=0.5, adverse_fill_bps=10, seed=7))
    b = FillEngine(FillParams(adverse_fill_prob=0.5, adverse_fill_bps=10, seed=7))
    pa = [a.market(+1, 1, bar(o=100), symbol_kind="etf", quoted_spread_bps=None).price for _ in range(20)]
    pb = [b.market(+1, 1, bar(o=100), symbol_kind="etf", quoted_spread_bps=None).price for _ in range(20)]
    assert pa == pb and len(set(pa)) == 2


def _broker(**kw):
    events = []
    b = SimBroker(100_000, on_trade_update=events.append, **kw)
    b.mark("SPY", 100.0, T0)
    return b, events


def test_market_order_fills_only_on_a_later_bar():
    b, ev = _broker()
    b.submit_market_order("SPY", 10, "buy", "c1", "day")
    b.step(bar(ts=T0, o=100.0))                      # same bar as the decision: must NOT fill
    assert b.orders["c1"].status == "new"
    b.step(bar(ts=T0 + timedelta(minutes=1), o=100.2))
    assert b.orders["c1"].status == "filled" and b.positions["SPY"] == 10
    assert [e.event for e in ev] == ["new", "fill"] and b.get_positions()["SPY"].qty == 10


def test_limit_order_respects_price_and_replace():
    b, _ = _broker()
    b.submit_limit_order("SPY", 10, "buy", 100.0, "c1", "day")
    b.step(bar(ts=T0 + timedelta(minutes=1), o=100.5))
    assert b.orders["c1"].status == "new"
    b.replace_order(b.orders["c1"].id, limit_price=100.60)
    b.step(bar(ts=T0 + timedelta(minutes=2), o=100.5))
    assert b.orders["c1"].status == "filled"


def test_oto_leg_goes_live_on_parent_fill_and_elects_on_gap():
    b, ev = _broker()
    b.submit_oto("SPY", 10, "buy", "e1", stop_price=98.0)
    assert [o.client_order_id for o in b.get_open_orders()] == ["e1"], "leg is held while the parent is open"
    b.step(bar(ts=T0 + timedelta(minutes=1), o=100.0))
    assert b.orders["e1"].status == "filled" and b.orders["e1-stop"].status == "new" and not b.orders["e1-stop"].held
    b.step(bar(ts=T0 + timedelta(minutes=2), o=99.0, l=97.5, c=98.5))    # elects
    assert b.orders["e1-stop"].elected and b.orders["e1-stop"].status == "new"
    b.step(bar(ts=T0 + timedelta(minutes=3), o=96.0, c=96.5))              # gap through: fills at the open
    assert b.orders["e1-stop"].status == "filled" and b.positions == {}
    assert b.orders["e1-stop"].avg_price == pytest.approx(96.0 * (1 - 5 / 1e4))


def test_bracket_cancels_sibling_and_fractional_constraints():
    b, _ = _broker()
    b.submit_bracket("SPY", 10, "buy", "e1", take_profit_price=101.0, stop_price=98.0)
    b.step(bar(ts=T0 + timedelta(minutes=1), o=100.0))
    b.step(bar(ts=T0 + timedelta(minutes=2), o=101.5, c=101.6))     # take profit limit fills
    assert b.orders["e1-tp"].status == "filled" and b.orders["e1-stop"].status == "canceled"
    with pytest.raises(ValueError):
        b.submit_market_order("SPY", 0.5, "buy", "f1", "opg")
    with pytest.raises(ValueError):
        b.submit_oto("SPY", 0.5, "buy", "f2", stop_price=90.0)
    with pytest.raises(DuplicateClientOrderId):
        b.submit_market_order("SPY", 1, "buy", "e1", "day")


def test_auction_orders_and_end_of_day_expiry():
    b, ev = _broker()
    b.submit_market_order("SPY", 10, "buy", "o1", "opg")
    b.submit_market_order("SPY", 5, "sell", "c1", "cls")
    b.submit_limit_order("SPY", 1, "buy", 1.0, "never", "day")
    b.submit_stop_order("SPY", 1, "sell", 50.0, "gtc1", "gtc")
    b.session_open("SPY", 100.0, T0)
    assert b.orders["o1"].status == "filled" and b.orders["o1"].avg_price == pytest.approx(100.0 * 1.0003)
    b.session_close("SPY", 101.0, T0 + timedelta(hours=6))
    assert b.orders["c1"].status == "filled" and b.orders["c1"].avg_price == pytest.approx(101.0 * 0.9999)
    b.end_of_day(T0 + timedelta(hours=6))
    assert b.orders["never"].status == "expired" and b.orders["gtc1"].status == "new"
