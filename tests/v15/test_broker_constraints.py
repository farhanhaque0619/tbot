"""Documented Alpaca constraints enforced before any API call, on the pure validator, the FakeBroker and the real
AlpacaBroker request builder (with a stub TradingClient: nothing leaves the process)."""
from datetime import datetime

import pytest

from bot.config import Settings
from bot.execution.broker import AlpacaBroker, OrderInfo
from bot.execution.constraints import OrderConstraintError, can_replace, round_to_tick, validate_order
from bot.execution.fake_broker import FakeAPIError, FakeBroker
from bot.data.calendar import NY

T_MID = datetime(2026, 9, 22, 11, 0, tzinfo=NY)
T_EVE = datetime(2026, 9, 22, 20, 0, tzinfo=NY)
T_1552 = datetime(2026, 9, 22, 15, 52, tzinfo=NY)
T_0927 = datetime(2026, 9, 22, 9, 27, tzinfo=NY)


def v(**kw):
    base = dict(symbol="SPY", qty=1, side="buy", order_type="market", tif="day")
    base.update(kw)
    return validate_order(**base)


def test_fractional_orders_are_day_market_or_limit_and_simple_only():
    v(qty=0.5)
    v(qty=0.5, order_type="limit", limit_price=100.0)
    v(qty=None, notional=25.0)
    for kw in (dict(qty=0.5, tif="gtc"), dict(qty=0.5, tif="opg"), dict(qty=0.5, tif="cls"), dict(qty=None, notional=5.0, tif="gtc"),
               dict(qty=0.5, order_type="stop", stop_price=90.0), dict(qty=0.5, order_class="oto", stop_price=90.0, base_price=100.0),
               dict(qty=0.5, order_type="limit", limit_price=100.0, extended_hours=True)):
        with pytest.raises(OrderConstraintError):
            v(**kw)


def test_extended_hours_needs_limit_day_or_gtc():
    v(order_type="limit", limit_price=100.0, extended_hours=True)
    v(order_type="limit", limit_price=100.0, extended_hours=True, tif="gtc")
    with pytest.raises(OrderConstraintError):
        v(extended_hours=True)
    with pytest.raises(OrderConstraintError):
        v(order_type="limit", limit_price=100.0, extended_hours=True, tif="ioc")


def test_opg_and_cls_acceptance_windows():
    v(tif="opg", now=T_0927)
    v(tif="opg", now=T_EVE)
    v(tif="opg")                             # no clock: the API decides
    with pytest.raises(OrderConstraintError, match="09:28"):
        v(tif="opg", now=T_MID)
    v(tif="cls", now=T_MID)
    v(tif="cls", now=T_EVE)
    with pytest.raises(OrderConstraintError, match="15:50"):
        v(tif="cls", now=T_1552)
    with pytest.raises(OrderConstraintError):
        v(tif="opg", order_type="stop", stop_price=90.0)


def test_bracket_and_oto_leg_distances_and_whole_shares():
    out = v(order_class="oto", stop_price=99.994, base_price=100.0)
    assert out["stop_price"] == 99.99
    with pytest.raises(OrderConstraintError, match="0.01 below"):
        v(order_class="oto", stop_price=99.996, base_price=100.0)
    with pytest.raises(OrderConstraintError):
        v(order_class="oto", stop_price=95.0)                # no base price
    with pytest.raises(OrderConstraintError):
        v(order_class="oto", stop_price=95.0, base_price=100.0, tif="opg")
    out = v(order_class="bracket", stop_price=95.0, take_profit_price=105.0, base_price=100.0)
    assert out == {"qty": 1, "notional": None, "stop_price": 95.0, "take_profit_price": 105.0}
    with pytest.raises(OrderConstraintError, match="take profit"):
        v(order_class="bracket", stop_price=95.0, take_profit_price=100.0, base_price=100.0)
    # sell side mirrors
    v(side="sell", order_class="bracket", stop_price=105.0, take_profit_price=95.0, base_price=100.0)
    with pytest.raises(OrderConstraintError):
        v(side="sell", order_class="oto", stop_price=95.0, base_price=100.0)
    # limit entry: the limit is the base
    out = v(order_type="limit", limit_price=100.123, order_class="oto", stop_price=95.0)
    assert out["limit_price"] == 100.12


def test_prices_round_to_tick_and_basic_field_checks():
    assert round_to_tick(123.456) == 123.46 and round_to_tick(0.12345) == 0.1235 and round_to_tick(1.0) == 1.0
    assert v(order_type="limit", limit_price=100.005)["limit_price"] in (100.0, 100.01)
    for kw in (dict(qty=0), dict(qty=-1), dict(side="hold"), dict(order_type="trailing"), dict(tif="week"), dict(order_class="oco", stop_price=1),
               dict(symbol="spy"), dict(order_type="limit"), dict(order_type="stop"), dict(limit_price=100.0), dict(qty=1, notional=5.0)):
        with pytest.raises(OrderConstraintError):
            v(**kw)


def test_can_replace_rules():
    ok = OrderInfo("1", "c", "SPY", "buy", 10, "new", 0, None, None, None)
    assert can_replace(ok) == (True, "")
    notional = OrderInfo("2", "c", "SPY", "buy", 0, "new", 0, None, None, None, notional=25.0)
    assert can_replace(notional)[0] is False
    done = OrderInfo("3", "c", "SPY", "buy", 10, "filled", 10, 100.0, None, None)
    assert can_replace(done)[0] is False


# ---------------------------------------------------------------------------------------------- FakeBroker
def test_fake_broker_enforces_the_same_constraints_and_models_legs():
    b = FakeBroker(prices={"SPY": 100.0})
    b.now = T_MID
    with pytest.raises(FakeAPIError):
        b.submit_limit_order("SPY", 0.5, "buy", 99.0, "c1", "gtc")
    with pytest.raises(FakeAPIError):
        b.submit_market_order("SPY", 1, "buy", "c2", "opg")            # 11:00 is outside the OPG window
    o = b.submit_oto("SPY", 10, "buy", "c3", stop_price=95.0)
    assert o.order_class == "oto" and len(o.legs) == 1 and b.orders["c3-stop"].status == "held"
    b.fill("c3")
    assert b.orders["c3-stop"].status == "new" and b.orders["c3-stop"].stop_price == 95.0 and b.positions["SPY"] == 10
    br = b.submit_bracket("SPY", 5, "buy", "c4", take_profit_price=110.0, stop_price=90.0)
    assert {leg.client_order_id for leg in br.legs} == {"c4-stop", "c4-tp"}
    b.cancel("c4")
    assert b.orders["c4-tp"].status == "canceled" and b.orders["c4-stop"].status == "canceled"
    lo = b.submit_limit_order("SPY", 3, "buy", 99.0, "c5", "day")
    r = b.replace_order(lo.id, limit_price=99.5)
    assert r.limit_price == 99.5 and r.id != lo.id and b.orders["c5"].id == r.id
    with pytest.raises(FakeAPIError):
        b.replace_order("does-not-exist")
    since = b.get_orders_since(T_MID.replace(hour=1))
    assert [x.client_order_id for x in since][:2] == ["c3", "c3-stop"] and all(x.submitted_at > T_MID.replace(hour=1) for x in since)


# ---------------------------------------------------------------------------------------------- AlpacaBroker
class _StubOrder:
    def __init__(self, req, oid="o1"):
        self.id, self.client_order_id, self.symbol, self.side = oid, req.client_order_id, req.symbol, req.side
        self.qty, self.status, self.filled_qty, self.filled_avg_price = req.qty, "new", 0, None
        self.submitted_at = self.filled_at = self.updated_at = None
        self.notional, self.time_in_force, self.order_type, self.type = None, req.time_in_force, req.type, req.type
        self.limit_price = getattr(req, "limit_price", None)
        self.stop_price = getattr(req, "stop_price", None)
        self.order_class = getattr(req, "order_class", None)
        self.extended_hours = getattr(req, "extended_hours", None)
        self.legs = []


class _StubClient:
    def __init__(self):
        self.requests = []
        self.replaced = []

    def submit_order(self, req):
        self.requests.append(req)
        return _StubOrder(req)

    def get_order_by_id(self, oid):
        from types import SimpleNamespace
        return SimpleNamespace(id=oid, client_order_id="c", symbol="SPY", side="buy", qty=10, status="new", filled_qty=0, filled_avg_price=None,
                               submitted_at=None, filled_at=None, notional=None, time_in_force="day", order_type="limit", type="limit", limit_price=99.0,
                               stop_price=None, order_class="simple", legs=None, updated_at=None, extended_hours=False)

    def replace_order_by_id(self, oid, req):
        self.replaced.append((oid, req))
        from types import SimpleNamespace
        return SimpleNamespace(id="o2", client_order_id="c", symbol="SPY", side="buy", qty=req.qty or 10, status="replaced", filled_qty=0, filled_avg_price=None,
                               submitted_at=None, filled_at=None, notional=None, time_in_force="day", order_type="limit", type="limit", limit_price=req.limit_price,
                               stop_price=None, order_class="simple", legs=None, updated_at=None, extended_hours=False)

    def get_orders(self, req):
        self.last_get = req
        return []


@pytest.fixture
def alpaca(monkeypatch):
    s = Settings(alpaca_paper_api_key="PKTESTTESTTESTTEST", alpaca_paper_secret_key="x" * 40, trading_env="paper")
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.env, b.is_paper, b.settings, b.client, b.base_url = "paper", True, s, _StubClient(), "https://paper-api.alpaca.markets"
    b.last_request_id, b.request_ids = None, []
    monkeypatch.setattr(AlpacaBroker, "_now_et", lambda self: T_EVE)
    return b


def test_alpaca_broker_builds_requests_and_refuses_documented_violations(alpaca):
    from alpaca.trading.enums import OrderClass, OrderType, TimeInForce
    o = alpaca.submit_limit_order("SPY", 10, "buy", 100.004, "c1", "day")
    req = alpaca.client.requests[-1]
    assert req.type == OrderType.LIMIT and req.limit_price == 100.0 and req.time_in_force == TimeInForce.DAY and o.limit_price == 100.0
    alpaca.submit_stop_order("SPY", 10, "sell", 95.0, "c2", "gtc")
    assert alpaca.client.requests[-1].type == OrderType.STOP and alpaca.client.requests[-1].stop_price == 95.0
    alpaca.submit_oto("SPY", 10, "buy", "c3", stop_price=95.0, base_price=100.0)
    req = alpaca.client.requests[-1]
    assert req.order_class == OrderClass.OTO and req.stop_loss.stop_price == 95.0 and req.type == OrderType.MARKET
    alpaca.submit_bracket("SPY", 10, "buy", "c4", take_profit_price=110.0, stop_price=95.0, entry_type="limit", limit_price=100.0)
    req = alpaca.client.requests[-1]
    assert req.order_class == OrderClass.BRACKET and req.take_profit.limit_price == 110.0 and req.limit_price == 100.0
    alpaca.submit_market_order("SPY", 1, "buy", "c5", "opg")                  # 20:00 ET: inside the OPG window
    with pytest.raises(OrderConstraintError):
        alpaca.submit_market_order("SPY", 0.5, "buy", "c6", "opg")
    with pytest.raises(OrderConstraintError):
        alpaca.submit_oto("SPY", 2.5, "buy", "c7", stop_price=95.0, base_price=100.0)
    with pytest.raises(OrderConstraintError):
        alpaca.submit_limit_order("SPY", 1, "buy", 100.0, "c8", "gtc", extended_hours=True) and alpaca.submit_market_order("SPY", 1, "buy", "c9", "day")  # noqa
        alpaca.submit_stop_order("SPY", 1, "sell", 0.0, "c10")
    n = len(alpaca.client.requests)
    with pytest.raises(OrderConstraintError):
        alpaca.submit_bracket("SPY", 10, "buy", "c11", take_profit_price=100.0, stop_price=95.0, base_price=100.0)
    assert len(alpaca.client.requests) == n, "nothing was sent for a refused order"
    r = alpaca.replace_order("o1", limit_price=99.5)
    assert alpaca.client.replaced[-1][1].limit_price == 99.5 and r.status == "replaced"
    with pytest.raises(OrderConstraintError):
        alpaca.replace_order("o1", qty=2.5)
    alpaca.get_orders_since(T_MID)
    assert alpaca.client.last_get.after == T_MID and alpaca.client.last_get.nested is True


def test_alpaca_market_order_outside_opg_window_is_refused_locally(monkeypatch, alpaca):
    monkeypatch.setattr(AlpacaBroker, "_now_et", lambda self: T_MID)
    with pytest.raises(OrderConstraintError, match="OPG"):
        alpaca.submit_market_order("SPY", 1, "buy", "c1", "opg")
    assert alpaca.client.requests == []
