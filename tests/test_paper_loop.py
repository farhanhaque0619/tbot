"""Restart safety and kill switch in the paper-trading loop (fake broker, in-memory cache)."""
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from bot.config import Settings
from bot.data.calendar import NY
from bot.data.loader import BarLoader
from bot.data.store import BarStore
from bot.execution import FakeBroker, PaperTrader, StateStore
from bot.execution.fake_broker import DuplicateClientOrderId
from bot.monitoring.alerts import Alerter
from bot.risk import RiskLimits
from bot.strategies import MACrossover
from tests.conftest import make_bars

FRI_EVENING = datetime(2024, 1, 5, 19, 30, tzinfo=NY)


@pytest.fixture
def env(tmp_path):
    closes = 100 + np.arange(300) * 0.2  # steady uptrend -> MA crossover wants to be long
    df = make_bars(300, seed=1, closes=closes)
    df.index = pd.bdate_range(end="2024-01-05", periods=300, tz=NY)
    store = BarStore()
    store.upsert_bars("SPY", df)
    store.set_coverage("SPY", df.index[0].date(), df.index[-1].date())
    broker = FakeBroker(cash=100_000, prices={"SPY": float(df["close"].iloc[-1])})
    settings = Settings(_env_file=None)
    alerter = Alerter()

    def make(limits=None):
        return PaperTrader(settings=settings, broker=broker, loader=BarLoader(store, None), bar_store=store,
                           strategy_cls=MACrossover, params={"fast": 10, "slow": 50}, symbols=["SPY"],
                           state_store=StateStore(tmp_path / "paper.json"), alerter=alerter,
                           risk_limits=limits or RiskLimits())
    return {"make": make, "broker": broker, "store": store, "df": df, "alerter": alerter}


def test_one_order_per_session_even_across_restarts(env):
    t = env["make"]()
    s1 = t.run_cycle(FRI_EVENING)
    assert s1["actions"] == ["buy 313 SPY (entry)"] or s1["actions"][0].startswith("buy")
    t.run_cycle(FRI_EVENING)
    env["make"]().run_cycle(FRI_EVENING)          # fresh process, same state file
    env["make"]().run_cycle(datetime(2024, 1, 6, 8, 0, tzinfo=NY))
    assert len(env["broker"].submitted) == 1
    assert env["broker"].submitted[0].client_order_id == "paper-SPY-2024-01-05-entry"


def test_crash_after_submit_before_state_save_does_not_double_order(env, tmp_path):
    t = env["make"]()
    # simulate: the broker accepted the order but the process died before the state file was updated
    real_submit = env["broker"].submit_market_order
    def crash(*a, **k):
        real_submit(*a, **k)
        raise RuntimeError("power cut")
    env["broker"].submit_market_order = crash
    with pytest.raises(RuntimeError):
        t.run_cycle(FRI_EVENING)
    env["broker"].submit_market_order = real_submit
    state = StateStore(tmp_path / "paper.json").load()
    assert state.orders["paper-SPY-2024-01-05-entry"]["status"] == "submitting"
    # restart: the loop finds the order at the broker by client id and adopts it instead of re-sending
    t2 = env["make"]()
    t2.run_cycle(FRI_EVENING)
    assert len(env["broker"].submitted) == 1
    assert t2.state.orders["paper-SPY-2024-01-05-entry"]["status"] == "new"


def test_broker_rejects_duplicate_client_order_id(env):
    b = env["broker"]
    b.submit_market_order("SPY", 1, "buy", "dup", "opg")
    with pytest.raises(DuplicateClientOrderId):
        b.submit_market_order("SPY", 1, "buy", "dup", "opg")


def test_fill_then_stop_then_trade_recorded(env):
    t = env["make"]()
    t.run_cycle(FRI_EVENING)
    env["broker"].fill_all()
    t.run_cycle(datetime(2024, 1, 6, 9, 0, tzinfo=NY))
    assert t.state.positions["SPY"]["entry_price"] is not None
    stop = t.state.positions["SPY"]["stop"]
    crash = stop * 0.95
    idx = pd.DatetimeIndex([pd.Timestamp("2024-01-08", tz=NY)])
    env["store"].upsert_bars("SPY", pd.DataFrame({"open": [crash], "high": [crash], "low": [crash], "close": [crash], "volume": [1e6]}, index=idx))
    env["store"].set_coverage("SPY", date(2020, 1, 1), date(2024, 1, 8))
    env["broker"].set_price("SPY", crash)
    s = t.run_cycle(datetime(2024, 1, 8, 19, 30, tzinfo=NY))
    assert s["actions"] == ["sell 313 SPY (exit)"] or s["actions"][0].startswith("sell")
    env["broker"].fill_all()
    t.run_cycle(datetime(2024, 1, 8, 20, 0, tzinfo=NY))
    assert not t.state.positions
    assert len(t.state.trades) == 1 and t.state.trades[0]["pnl"] < 0
    assert t.state.trades[0]["exit_reason"].startswith("stop")
    assert t.state.risk_exits["SPY"] == ["2024-01-08"]


def test_kill_switch_liquidates_and_blocks_trading(env):
    t = env["make"](RiskLimits(max_drawdown_pct=0.10))
    t.run_cycle(FRI_EVENING)
    env["broker"].fill_all()
    t.run_cycle(datetime(2024, 1, 6, 9, 0, tzinfo=NY))
    assert env["broker"].positions.get("SPY", 0) > 0
    env["broker"].set_price("SPY", env["broker"].prices["SPY"] * 0.7)   # -30% gap
    s = t.run_cycle(datetime(2024, 1, 8, 19, 30, tzinfo=NY))
    assert s["status"] == "killed" and "kill_switch" in s["actions"]
    assert env["broker"].positions == {}, "everything liquidated"
    assert t.state.risk["killed"]
    assert any("KILL SWITCH" in m for m in env["alerter"].sent)
    # a restart keeps the kill switch
    t2 = env["make"](RiskLimits(max_drawdown_pct=0.10))
    assert t2.risk.killed
    n = len(env["broker"].submitted)
    assert t2.run_cycle(datetime(2024, 1, 9, 19, 30, tzinfo=NY))["status"] == "killed"
    assert len(env["broker"].submitted) == n


def test_waits_for_opg_window_when_market_closed(env):
    t = env["make"]()
    s = t.run_cycle(datetime(2024, 1, 5, 17, 0, tzinfo=NY))   # after close, before 19:00 -> OPG not accepted yet
    assert s["status"] == "waiting_for_order_window" and not env["broker"].submitted
    s = t.run_cycle(FRI_EVENING)
    assert len(env["broker"].submitted) == 1 and env["broker"].submitted[0].client_order_id.endswith("entry")


def test_state_file_is_written_atomically(tmp_path):
    from bot.execution.state import BotState
    st = StateStore(tmp_path / "s.json")
    st.save(BotState(strategy="x", last_processed={"SPY": "2024-01-05"}))
    assert st.load().last_processed == {"SPY": "2024-01-05"}
    assert not list(tmp_path.glob("*.tmp"))
