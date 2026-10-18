"""V1.5 order-type integration tests against the REAL Alpaca PAPER API (gated: RUN_ALPACA_INTEGRATION=1 and
RUN_ALPACA_INTEGRATION_ORDERS=1). Everything is cancelled at the end; whole-share tests need enough paper buying power for
one share of the cheapest liquid ETF configured (default XLF). Documented constraints are verified against the API and
recorded in V1_5_AUDIT.md when they differ."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from bot.data.calendar import NY
from bot.execution.constraints import OrderConstraintError

SYMBOL = os.environ.get("INTEGRATION_WHOLE_SHARE_SYMBOL", "XLF")


@pytest.fixture
def cid():
    return f"v15test-{int(time.time() * 1000)}"


def _far_limit(broker, symbol, side):
    q = broker.get_latest_quote(symbol)
    px = q.mid if q else 10.0
    return round(px * (0.5 if side == "buy" else 1.5), 2)


def _cleanup(broker, orders):
    for o in orders:
        try:
            broker.cancel_order(o.id)
        except Exception:  # noqa: BLE001
            pass


def test_fractional_rejections_are_caught_locally(broker, orders_enabled, cid):
    with pytest.raises(OrderConstraintError):
        broker.submit_limit_order("SPY", 0.01, "buy", 1.0, cid + "-a", "gtc")
    with pytest.raises(OrderConstraintError):
        broker.submit_oto("SPY", 0.01, "buy", cid + "-b", stop_price=1.0, base_price=2.0)
    with pytest.raises(OrderConstraintError):
        broker.submit_market_order("SPY", 0.01, "buy", cid + "-c", "opg")


def test_limit_stop_replace_cancel_whole_share(broker, orders_enabled, cid):
    lp = _far_limit(broker, SYMBOL, "buy")
    o = broker.submit_limit_order(SYMBOL, 1, "buy", lp, cid + "-lim", "day")
    created = [o]
    try:
        assert o.order_type == "limit" and o.limit_price == lp and o.qty == 1
        live = broker.get_order_by_client_id(cid + "-lim")
        if live is not None and live.status in ("new", "partially_filled"):
            r = broker.replace_order(o.id, limit_price=round(lp * 0.99, 2))
            created.append(r)
            assert r.limit_price == round(lp * 0.99, 2)
        else:
            # outside market hours the order is 'accepted' and Alpaca rejects a replace (42210000); we refuse locally
            with pytest.raises(OrderConstraintError, match="accepted"):
                broker.replace_order(o.id, limit_price=round(lp * 0.99, 2))
        st = broker.submit_stop_order(SYMBOL, 1, "sell", round(lp * 0.5, 2), cid + "-stp", "gtc")
        created.append(st)
        assert st.order_type == "stop" and st.time_in_force == "gtc"
        since = broker.get_orders_since(datetime.now(timezone.utc) - timedelta(minutes=5))
        assert {x.client_order_id for x in since} >= {cid + "-stp"}
    finally:
        _cleanup(broker, created)


def test_oto_and_bracket_nested_legs(broker, orders_enabled, cid):
    lp = _far_limit(broker, SYMBOL, "buy")
    created = []
    try:
        o = broker.submit_oto(SYMBOL, 1, "buy", cid + "-oto", stop_price=round(lp * 0.8, 2), entry_type="limit", limit_price=lp)
        created.append(o)
        assert o.order_class == "oto" and len(o.legs) == 1 and o.legs[0].order_type == "stop" and o.legs[0].status in ("held", "new", "accepted")
        b = broker.submit_bracket(SYMBOL, 1, "buy", cid + "-brk", take_profit_price=round(lp * 1.5, 2), stop_price=round(lp * 0.8, 2), entry_type="limit", limit_price=lp)
        created.append(b)
        assert b.order_class == "bracket" and len(b.legs) == 2
        # auction TIFs with an OTO: the tables say unsupported; verify the API answer and record it
        try:
            x = broker.submit_oto(SYMBOL, 1, "buy", cid + "-otoopg", stop_price=round(lp * 0.8, 2), entry_type="limit", limit_price=lp, tif="opg")
            created.append(x)
            pytest.fail("OTO with OPG was ACCEPTED by the API: record in V1_5_AUDIT.md and relax the local check")
        except OrderConstraintError:
            pass
    finally:
        _cleanup(broker, created)


def test_opg_cls_windows_match_the_api(broker, orders_enabled, cid):
    now = broker.get_clock().timestamp.astimezone(NY).time()
    lp = _far_limit(broker, SYMBOL, "buy")
    created = []
    try:
        for tif in ("opg", "cls"):
            try:
                o = broker.submit_limit_order(SYMBOL, 1, "buy", lp, f"{cid}-{tif}", tif)
                created.append(o)
                assert o.time_in_force == tif
            except OrderConstraintError as e:
                assert ("09:28" in str(e)) or ("15:50" in str(e)), f"unexpected local refusal at {now}: {e}"
    finally:
        _cleanup(broker, created)


def test_fractional_day_stop_is_accepted(broker, orders_enabled, cid):
    """Spec §8 protects a fractional position with a DAY stop; verify the API accepts a fractional stop order at all
    (a far-away buy stop needs no position). A rejection here means D29 in V1_5_AUDIT.md must change."""
    q = broker.get_latest_quote("SPY")
    px = q.mid if q else 500.0
    o = broker.submit_stop_order("SPY", 0.1, "buy", round(px * 1.5, 2), cid + "-fstop", "day")
    try:
        assert o.order_type == "stop" and o.qty == pytest.approx(0.1) and o.time_in_force == "day" and o.status in ("new", "accepted", "pending_new", "held")
    finally:
        _cleanup(broker, [o])
