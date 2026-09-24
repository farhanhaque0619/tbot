"""Phase 3/16 failure modes against the fake broker: partial fills, rejections, cancels, network errors,
rate limits, restarts around orders and fills, stale data, holidays/weekends, invalid symbols."""
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
import requests

from bot.config import Settings
from bot.data.calendar import NY
from bot.data.loader import BarLoader
from bot.data.store import BarStore
from bot.execution import FakeBroker, StateStore, Trader
from bot.execution.fake_broker import FakeAPIError
from bot.monitoring.alerts import Alerter
from bot.monitoring.decisions import DecisionLog
from bot.utils.retry import with_retry
from tests.conftest import make_bars

FRI_EVENING = datetime(2024, 1, 5, 19, 30, tzinfo=NY)
MON_OPEN = datetime(2024, 1, 8, 10, 0, tzinfo=NY)


@pytest.fixture
def env(tmp_path):
    closes = 100 + np.arange(300) * 0.2
    df = make_bars(300, seed=1, closes=closes)
    df.index = pd.bdate_range(end="2024-01-05", periods=300, tz=NY)
    store = BarStore()
    store.upsert_bars("SPY", df)
    store.set_coverage("SPY", df.index[0].date(), df.index[-1].date())
    broker = FakeBroker(cash=100_000, prices={"SPY": float(df["close"].iloc[-1])})
    settings = Settings(_env_file=None, allow_fractional=False)
    alerter = Alerter()

    def make(symbols=("SPY",)):
        return Trader(settings=settings, broker=broker, loader=BarLoader(store, None), bar_store=store, strategy_cls=__import__("bot.strategies", fromlist=["MACrossover"]).MACrossover,
                      params={"fast": 10, "slow": 50}, symbols=list(symbols), state_store=StateStore(tmp_path / "paper.json"),
                      alerter=alerter, decision_log=DecisionLog(tmp_path / "d.jsonl"))
    return {"make": make, "broker": broker, "store": store, "df": df, "alerter": alerter, "tmp": tmp_path}


def _enter(env):
    t = env["make"]()
    t.run_cycle(FRI_EVENING)
    assert len(env["broker"].submitted) == 1
    return t, env["broker"].submitted[0].client_order_id


def test_partial_fill_then_fill_books_once_with_avg_price(env):
    t, cid = _enter(env)
    b = env["broker"]
    b.partial_fill(cid, qty=100, price=150.0)
    t.run_cycle(datetime(2024, 1, 6, 8, 0, tzinfo=NY))
    assert t.state.orders[cid]["status"] == "partially_filled" and t.state.orders[cid]["filled_qty"] == 100
    assert any("partial fill" in m for m in env["alerter"].sent)
    assert t.state.positions["SPY"]["entry_price"] is None       # not booked until terminal
    b.fill(cid, price=152.0)
    t.run_cycle(datetime(2024, 1, 6, 9, 0, tzinfo=NY))
    p = t.state.positions["SPY"]
    assert p["qty"] == b.orders[cid].qty and 150.0 < p["entry_price"] < 152.0
    assert t.state.orders[cid]["realized_slippage_bps"] is not None


def test_partial_fill_then_cancel_keeps_the_filled_shares(env):
    t, cid = _enter(env)
    b = env["broker"]
    b.partial_fill(cid, qty=10)
    b.cancel(cid)
    t.run_cycle(datetime(2024, 1, 6, 9, 0, tzinfo=NY))
    assert t.state.orders[cid]["status"] == "canceled"
    assert "SPY" in t.state.positions and t.state.positions["SPY"]["qty"] == 10, "10 shares were bought; local record must reflect the broker"
    assert b.positions["SPY"] == 10


def test_rejected_order_alerts_and_clears_local_position(env):
    env["broker"].reject_next("insufficient buying power")
    t = env["make"]()
    t.run_cycle(FRI_EVENING)
    cid = env["broker"].submitted[0].client_order_id
    assert t.state.orders[cid]["status"] == "rejected" and "SPY" not in t.state.positions
    assert any("rejected" in m for m in env["alerter"].sent)
    # the session stays processed: no retry storm
    t.run_cycle(datetime(2024, 1, 5, 20, 0, tzinfo=NY))
    assert len(env["broker"].submitted) == 1


def test_insufficient_buying_power_modelled_by_fake_broker(env):
    env["broker"].cash = 10.0
    t = env["make"]()
    t.run_cycle(FRI_EVENING)
    assert not env["broker"].submitted, "sizing must produce zero shares with $10 of cash and whole-share sizing"


def test_canceled_entry_drops_local_position(env):
    t, cid = _enter(env)
    env["broker"].cancel(cid)
    t.run_cycle(datetime(2024, 1, 6, 9, 0, tzinfo=NY))
    assert t.state.orders[cid]["status"] == "canceled" and "SPY" not in t.state.positions


def test_invalid_symbol_is_a_non_retryable_error(env):
    t = env["make"](symbols=("SPY", "NOPE"))
    with pytest.raises(RuntimeError, match="no cached bars"):
        t.run_cycle(FRI_EVENING)
    assert t.state.last_error and "NOPE" in t.state.last_error
    recs = DecisionLog(env["tmp"] / "d.jsonl").read()
    assert recs[-1]["symbol"] == "NOPE" and recs[-1]["exception"]


def test_network_failure_is_retried_then_cycle_fails_cleanly(env):
    b = env["broker"]
    t = env["make"]()
    b.fail_next(requests.ConnectionError("boom"))   # the fake has no retry layer; AlpacaBroker retries via with_retry (tested below)
    with pytest.raises(requests.ConnectionError):
        t.run_cycle(FRI_EVENING)
    assert t.state.last_error and "ConnectionError" in t.state.last_error
    assert not b.submitted
    # recovery on the next cycle
    t.run_cycle(FRI_EVENING)
    assert len(b.submitted) == 1


def test_with_retry_backs_off_on_429_and_5xx_but_not_4xx():
    calls = {"n": 0}
    sleeps = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeAPIError("rate limited", 429)
        return "ok"
    assert with_retry(flaky, attempts=5, base_delay=1, sleep=sleeps.append) == "ok"
    assert calls["n"] == 3 and sleeps and sleeps[1] > sleeps[0]

    def bad():
        raise FakeAPIError("bad request", 422)
    with pytest.raises(FakeAPIError):
        with_retry(bad, attempts=5, sleep=lambda s: None)

    def server():
        raise FakeAPIError("gateway", 502)
    n = {"c": 0}
    def counted():
        n["c"] += 1
        raise FakeAPIError("gateway", 502)
    with pytest.raises(FakeAPIError):
        with_retry(counted, attempts=3, sleep=lambda s: None)
    assert n["c"] == 3


def test_restart_while_order_pending_adopts_it(env):
    t, cid = _enter(env)
    t2 = env["make"]()
    t2.run_cycle(datetime(2024, 1, 5, 21, 0, tzinfo=NY))
    assert len(env["broker"].submitted) == 1 and t2.state.orders[cid]["status"] == "new"


def test_restart_after_fill_books_position(env):
    t, cid = _enter(env)
    env["broker"].fill_all()
    t2 = env["make"]()
    t2.run_cycle(datetime(2024, 1, 6, 9, 0, tzinfo=NY))
    assert t2.state.positions["SPY"]["entry_price"] is not None
    assert len(env["broker"].submitted) == 1


def test_stale_state_recovery_adopts_broker_positions(env, tmp_path):
    t, cid = _enter(env)
    env["broker"].fill_all()
    (tmp_path / "paper.json").unlink()                # state lost entirely
    t2 = env["make"]()
    s = t2.run_cycle(datetime(2024, 1, 8, 19, 30, tzinfo=NY))
    assert t2.state.positions["SPY"]["reason"] == "adopted" and t2.state.positions["SPY"]["stop"] is None
    assert any("did not open" in m or "adopted" in m for m in [t2.state.positions["SPY"]["reason"]])
    assert s["actions"] == [], "desired long == actual long: nothing to do, no duplicate entry"


def test_weekend_and_holiday_resolve_to_last_completed_session(env):
    from bot.data.calendar import SessionInfo, last_completed_session_date
    sat = datetime(2024, 1, 6, 12, 0, tzinfo=NY)
    assert last_completed_session_date(sat) == date(2024, 1, 5)
    # MLK day 2024-01-15: broker calendar has no session that day
    sessions = [SessionInfo(date(2024, 1, 12), datetime(2024, 1, 12, 9, 30, tzinfo=NY), datetime(2024, 1, 12, 16, 0, tzinfo=NY), "alpaca"),
                SessionInfo(date(2024, 1, 16), datetime(2024, 1, 16, 9, 30, tzinfo=NY), datetime(2024, 1, 16, 16, 0, tzinfo=NY), "alpaca")]
    assert last_completed_session_date(datetime(2024, 1, 15, 20, 0, tzinfo=NY), sessions) == date(2024, 1, 12)
    assert last_completed_session_date(datetime(2024, 1, 16, 12, 0, tzinfo=NY), sessions) == date(2024, 1, 12)
    assert last_completed_session_date(datetime(2024, 1, 16, 16, 30, tzinfo=NY), sessions) == date(2024, 1, 16)


def test_missing_bar_for_session_waits_without_marking_processed(env):
    t = env["make"]()
    s = t.run_cycle(datetime(2024, 1, 8, 19, 30, tzinfo=NY))    # Monday session, but no Monday bar in the cache
    assert s["actions"] == [] and "SPY" not in t.state.last_processed
    recs = DecisionLog(env["tmp"] / "d.jsonl").read()
    assert recs[-1]["order_decision"] == "waiting" and "not available" in recs[-1]["notes"][0]


def test_insufficient_warmup_is_logged_and_never_trades(env, tmp_path):
    store = BarStore()
    df = env["df"].tail(20)
    store.upsert_bars("SPY", df)
    store.set_coverage("SPY", date(2020, 1, 1), df.index[-1].date())
    from bot.strategies import MACrossover
    t = Trader(settings=Settings(_env_file=None), broker=env["broker"], loader=BarLoader(store, None), bar_store=store,
               strategy_cls=MACrossover, params={"fast": 10, "slow": 50}, symbols=["SPY"],
               state_store=StateStore(tmp_path / "w.json"), decision_log=DecisionLog(tmp_path / "w.jsonl"))
    t.run_cycle(FRI_EVENING)
    assert not env["broker"].submitted
    recs = DecisionLog(tmp_path / "w.jsonl").read()
    assert "insufficient warm-up" in recs[-1]["notes"][0]


def test_stale_quote_blocks_day_order_but_not_opg(env):
    b = env["broker"]
    from datetime import timedelta
    b.quote_age = timedelta(hours=3)
    b.now = MON_OPEN
    t = env["make"]()
    # Monday 10:00 -> whole shares -> OPG window closed, market open -> DAY order -> quote must be fresh -> blocked
    t.run_cycle(MON_OPEN)
    recs = DecisionLog(env["tmp"] / "d.jsonl").read()
    # No Monday bar exists yet in the cache so the session is Friday's; the decision is made with a stale quote.
    assert not b.submitted and recs[-1]["order_decision"] == "blocked" and recs[-1]["risk_decision"]["code"] == "data_fresh"
    assert any("stale market data" in m for m in env["alerter"].sent)


def test_malformed_quote_is_tolerated(env):
    b = env["broker"]
    b.get_latest_quote = lambda sym: (_ for _ in ()).throw(FakeAPIError("malformed", 500))
    t = env["make"]()
    t.run_cycle(FRI_EVENING)
    assert len(b.submitted) == 1, "OPG order does not need a quote"
