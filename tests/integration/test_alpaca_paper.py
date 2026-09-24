"""Phase 3: paper execution testing against the real Alpaca paper API (see conftest for enabling)."""
import time
import uuid
from datetime import date, timedelta

import pytest

from bot.data.calendar import now_ny

PROBE = "SPY"


# ------------------------------------------------------------------ read-only
def test_account_and_env(broker):
    a = broker.get_account()
    assert a.is_paper_account_number and a.status.upper() == "ACTIVE"
    assert a.equity > 0 and broker.last_request_id, "request id should be captured from response headers"
    print(f"\npaper account …{a.account_number[-4:]} equity={a.equity:.2f} cash={a.cash:.2f} bp={a.buying_power:.2f} "
          f"blocked={a.trading_blocked}/{a.account_blocked} shorting={a.shorting_enabled} req={broker.last_request_id}")


def test_clock_and_calendar(broker):
    c = broker.get_clock()
    assert c.next_open > c.timestamp - timedelta(days=1)
    sessions = broker.get_sessions(date.today() - timedelta(days=14), date.today() + timedelta(days=7))
    assert len(sessions) >= 8 and all(s.source == "alpaca" for s in sessions)


def test_bars_quote_asset(broker, settings):
    from bot.data.loader import BarLoader
    from bot.data.providers import AlpacaBarProvider
    from bot.data.store import BarStore
    loader = BarLoader(BarStore(), AlpacaBarProvider(settings, env="paper"), adjustment=settings.data_adjustment)
    df = loader.get_daily(PROBE, date.today() - timedelta(days=40), date.today())
    assert len(df) >= 20 and str(df.index.tz) == "America/New_York"
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    q = broker.get_latest_quote(PROBE)
    assert q is not None and q.ask >= q.bid > 0
    a = broker.get_asset(PROBE)
    assert a.tradable and a.fractionable and a.asset_class == "us_equity"
    with pytest.raises(Exception):
        broker.get_asset("THIS-IS-NOT-A-SYMBOL-123")


def test_open_orders_and_positions_read(broker):
    assert isinstance(broker.get_positions(), dict)
    assert isinstance(broker.get_open_orders(), list)


# --------------------------------------------------------------- order tests
@pytest.fixture
def cid():
    return f"itest-{uuid.uuid4().hex[:12]}"


def _wait(broker, cid, states, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        o = broker.get_order_by_client_id(cid)
        if o is not None and o.status in states:
            return o
        time.sleep(3)
    return broker.get_order_by_client_id(cid)


def test_submit_read_duplicate_cancel(broker, orders_enabled, cid):
    """Submit a tiny fractional DAY order, read it back by client id, prove the duplicate is refused, cancel it."""
    q = broker.get_latest_quote(PROBE)
    qty = round(max(1.5 / q.ask, 0.001), 3)       # about $1.50 notional (Alpaca minimum is $1)
    o = broker.submit_market_order(PROBE, qty, "buy", cid, "day")
    assert o.client_order_id == cid and o.qty == pytest.approx(qty)
    again = broker.submit_market_order(PROBE, qty, "buy", cid, "day")
    assert again.id == o.id, "duplicate client_order_id must resolve to the existing order, not a new one"
    read = broker.get_order_by_client_id(cid)
    assert read is not None and read.id == o.id
    if broker.get_clock().is_open:
        f = _wait(broker, cid, {"filled", "canceled", "rejected"})
        print(f"\norder {cid}: {f.status} filled={f.filled_qty} @ {f.filled_avg_price} req={broker.last_request_id}")
        if f.status == "filled":
            exit_cid = cid + "-exit"
            ex = broker.submit_market_order(PROBE, f.filled_qty, "sell", exit_cid, "day")
            fe = _wait(broker, exit_cid, {"filled", "canceled", "rejected"})
            assert fe.status == "filled", fe
    else:
        broker.cancel_order(o.id)
        c = _wait(broker, cid, {"canceled", "filled"}, timeout=30)
        assert c.status in ("canceled", "filled")


def test_rejections(broker, orders_enabled, cid):
    with pytest.raises(Exception):
        broker.submit_market_order("NOSUCHSYMBOLXYZ", 1, "buy", cid + "-bad", "day")
    with pytest.raises(Exception):
        broker.submit_market_order(PROBE, 1_000_000, "buy", cid + "-big", "day")   # insufficient buying power (HTTP 403)
    with pytest.raises(ValueError):
        broker.submit_market_order(PROBE, 0.5, "buy", cid + "-frac", "opg")        # fractional needs DAY


def test_trader_restart_safety_against_real_paper(broker, orders_enabled, settings, tmp_path):
    """Run the real Trader twice for the same session; exactly one order may exist at the broker."""
    from bot.data.loader import BarLoader
    from bot.data.providers import AlpacaBarProvider
    from bot.data.store import BarStore
    from bot.execution import StateStore, Trader
    from bot.monitoring.decisions import DecisionLog
    from bot.strategies import MACrossover

    store = BarStore(tmp_path / "bars.duckdb")
    loader = BarLoader(store, AlpacaBarProvider(settings, env="paper"), adjustment=settings.data_adjustment)
    run_id = f"itest-{uuid.uuid4().hex[:6]}"

    def make():
        return Trader(settings=settings, broker=broker, loader=loader, bar_store=store, strategy_cls=MACrossover,
                      params={"fast": 5, "slow": 10}, symbols=[PROBE], state_store=StateStore(tmp_path / f"{run_id}.json"),
                      run_id=run_id, decision_log=DecisionLog(tmp_path / "d.jsonl"))
    s1 = make().run_cycle(now_ny())
    s2 = make().run_cycle(now_ny())
    print(f"\ncycle1={s1}\ncycle2={s2}")
    mine = [o for o in broker.get_open_orders() if o.client_order_id.startswith(run_id)]
    assert len(mine) <= 1
    for o in mine:
        broker.cancel_order(o.id)
