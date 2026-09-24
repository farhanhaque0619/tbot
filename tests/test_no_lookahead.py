"""The backtester must never let information from bar t+1.. influence decisions at bar t."""
import numpy as np
import pandas as pd
import pytest

from bot.backtest import Backtester, CostModel
from bot.risk import RiskLimits
from bot.strategies import MACrossover, MeanReversion
from bot.strategies.base import Bar, Signal, Strategy
from tests.conftest import make_bars

NO_COST = CostModel(0, 0, 0)
LOOSE_RISK = RiskLimits(risk_per_trade_pct=0.05, max_position_pct=1.0, daily_loss_limit_pct=0.5, max_drawdown_pct=0.9)


@pytest.mark.parametrize("strategy_factory", [lambda: MACrossover(fast=5, slow=20), lambda: MeanReversion(lookback=10)])
def test_future_bars_do_not_change_past_decisions(strategy_factory):
    """Perturb everything after bar k; equity and trades up to k must be identical."""
    df = make_bars(400, seed=1)
    k = 250
    perturbed = df.copy()
    rng = np.random.default_rng(99)
    factor = np.exp(rng.normal(0, 0.05, len(df) - k)).cumprod()
    for c in ("open", "high", "low", "close"):
        perturbed.iloc[k:, perturbed.columns.get_loc(c)] *= factor
    a = Backtester(strategy_factory, costs=NO_COST, risk=LOOSE_RISK, benchmark=False).run({"X": df})
    b = Backtester(strategy_factory, costs=NO_COST, risk=LOOSE_RISK, benchmark=False).run({"X": perturbed})
    pd.testing.assert_series_equal(a.equity.iloc[:k], b.equity.iloc[:k])
    cutoff = df.index[k - 1]
    ta = [(t.entry_ts, t.exit_ts, t.qty) for t in a.trades if t.exit_ts <= cutoff]
    tb = [(t.entry_ts, t.exit_ts, t.qty) for t in b.trades if t.exit_ts <= cutoff]
    assert ta == tb
    # and the perturbation genuinely changed the future (the test isn't vacuous)
    assert not a.equity.iloc[k:].equals(b.equity.iloc[k:])


class SignalAtBar(Strategy):
    """Emits long at a fixed bar index, flat 5 bars later. Used to pin down fill timing."""
    name = "signal_at_bar"
    default_params = {"at": 10}

    @property
    def warmup(self): return 0
    def reset(self): self.i = -1
    def on_bar(self, bar: Bar):
        self.i += 1
        if self.i == self.params["at"]:
            return Signal(bar.symbol, 1, "go")
        if self.i == self.params["at"] + 5:
            return Signal(bar.symbol, 0, "stop")
        return None


def test_signal_fills_at_next_bars_open_not_this_close():
    df = make_bars(40, seed=2)
    at = 10
    res = Backtester(lambda: SignalAtBar(at=at), costs=NO_COST, risk=LOOSE_RISK, benchmark=False).run({"X": df})
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_ts == df.index[at + 1], "entry must be on the bar AFTER the signal"
    assert t.entry_price == pytest.approx(df["open"].iloc[at + 1]), "entry must be at that bar's OPEN"
    assert t.entry_price != pytest.approx(df["close"].iloc[at]), "and never at the signal bar's close"
    assert t.exit_ts == df.index[at + 6]
    assert t.exit_price == pytest.approx(df["open"].iloc[at + 6])


def test_equity_on_signal_bar_is_unchanged_by_the_signal():
    """A signal at bar t has no P&L impact on bar t (position opens at t+1)."""
    df = make_bars(40, seed=3)
    res = Backtester(lambda: SignalAtBar(at=10), costs=NO_COST, risk=LOOSE_RISK, benchmark=False).run({"X": df})
    assert res.equity.iloc[10] == res.equity.iloc[0]
    assert res.equity.iloc[11] != res.equity.iloc[10]


class Peeker(Strategy):
    """Tries to cheat: it only ever receives Bar objects, so the best it can do is remember the past."""
    name = "peeker"
    default_params = {}

    @property
    def warmup(self): return 0
    def reset(self): self.seen = []
    def on_bar(self, bar: Bar):
        self.seen.append(bar.ts)
        return None


def test_strategy_only_ever_sees_completed_bars_in_order():
    df = make_bars(50, seed=4)
    holder = {}
    def factory():
        holder["s"] = Peeker()
        return holder["s"]
    Backtester(factory, benchmark=False).run({"X": df})
    assert holder["s"].seen == list(df.index)


def test_trade_start_warmup_bars_are_never_traded():
    df = make_bars(300, seed=5)
    start = df.index[200]
    res = Backtester(lambda: MACrossover(fast=5, slow=20), costs=NO_COST, risk=LOOSE_RISK, benchmark=False,
                     trade_start=start).run({"X": df})
    assert all(t.entry_ts > start for t in res.trades)
    assert res.equity.index[0] == start


def test_walk_forward_selects_params_on_train_only():
    from bot.backtest import walk_forward
    df = make_bars(1500, seed=6)
    wf = walk_forward(MACrossover, {"X": df}, train_years=2, test_years=1, costs=NO_COST, risk=LOOSE_RISK)
    assert len(wf.folds) >= 2
    for f in wf.folds:
        assert f.train_end < f.test_start
        # the chosen params are the argmax of train sharpe among eligible grid entries
        eligible = [(p, m) for p, m in f.grid_results if m["trade_count"] >= 5] or f.grid_results
        best = max(eligible, key=lambda pm: pm[1]["sharpe"])[0]
        assert f.best_params == best
        assert f.test_result.equity.index[0] >= f.test_start


# ------------------------------------------------------------------ Phase 2 additions
def test_multi_symbol_future_mutation_does_not_change_past():
    from bot.strategies import MACrossover
    data = {s: make_bars(400, seed=i) for i, s in enumerate(("A", "B", "C"))}
    k = 250
    rng = np.random.default_rng(5)
    mutated = {}
    for s, df in data.items():
        m = df.copy()
        f = np.exp(rng.normal(0, 0.1, len(df) - k)).cumprod()
        for c in ("open", "high", "low", "close"):
            m.iloc[k:, m.columns.get_loc(c)] *= f
        m.iloc[k:, m.columns.get_loc("volume")] = 1.0
        mutated[s] = m
    a = Backtester(lambda: MACrossover(fast=5, slow=20), costs=NO_COST, risk=LOOSE_RISK, benchmark=True).run(data)
    b = Backtester(lambda: MACrossover(fast=5, slow=20), costs=NO_COST, risk=LOOSE_RISK, benchmark=True).run(mutated)
    pd.testing.assert_series_equal(a.equity.iloc[:k], b.equity.iloc[:k])
    pd.testing.assert_series_equal(a.benchmark_equity.iloc[:k], b.benchmark_equity.iloc[:k])
    cutoff = data["A"].index[k - 1]
    assert [(t.symbol, t.entry_ts, t.qty) for t in a.trades if t.entry_ts <= cutoff] == \
           [(t.symbol, t.entry_ts, t.qty) for t in b.trades if t.entry_ts <= cutoff]


def test_trading_loop_decision_is_unchanged_by_future_bars(tmp_path):
    """The loop's decision for session d must not depend on bars after d (appended or mutated)."""
    from datetime import datetime
    from bot.config import Settings
    from bot.data.calendar import NY
    from bot.data.loader import BarLoader
    from bot.data.store import BarStore
    from bot.execution import FakeBroker, StateStore, Trader
    from bot.monitoring.decisions import DecisionLog
    from bot.strategies import MACrossover

    def run(extra_future: pd.DataFrame | None, tag: str):
        df = make_bars(300, seed=9)
        df.index = pd.bdate_range(end="2024-01-05", periods=300, tz=NY)
        store = BarStore()
        store.upsert_bars("SPY", df)
        end = df.index[-1].date()
        if extra_future is not None:
            store.upsert_bars("SPY", extra_future)
            end = extra_future.index[-1].date()
        store.set_coverage("SPY", df.index[0].date(), end)
        broker = FakeBroker(cash=100_000, prices={"SPY": float(df["close"].iloc[-1])})
        t = Trader(settings=Settings(_env_file=None, allow_fractional=False), broker=broker, loader=BarLoader(store, None), bar_store=store,
                   strategy_cls=MACrossover, params={"fast": 10, "slow": 50}, symbols=["SPY"],
                   state_store=StateStore(tmp_path / f"{tag}.json"), decision_log=DecisionLog(tmp_path / f"{tag}.jsonl"))
        t.run_cycle(datetime(2024, 1, 5, 19, 30, tzinfo=NY))            # decide for session 2024-01-05
        rec = DecisionLog(tmp_path / f"{tag}.jsonl").read()[-1]
        return rec["signal"], rec["desired_position"], rec["order_decision"], rec["market_state"]

    base = run(None, "base")
    fut_idx = pd.bdate_range("2024-01-08", periods=30, tz=NY)
    crash = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1e9}, index=fut_idx)
    with_future = run(crash, "future")
    assert base == with_future


def test_market_state_uses_only_bars_up_to_decision(tmp_path):
    from datetime import datetime
    from bot.data.calendar import NY
    from bot.execution.market_state import build_market_state
    df = make_bars(300, seed=10)
    df.index = pd.bdate_range(end="2024-01-05", periods=300, tz=NY)
    now = datetime(2024, 1, 5, 19, 30, tzinfo=NY)
    a = build_market_state(symbol="X", bars=df, now=now, market_open=False, quote=None, account=None, positions={}, open_orders=[])
    fut = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1e9},
                       index=pd.bdate_range("2024-01-08", periods=30, tz=NY))
    full = pd.concat([df, fut])
    b = build_market_state(symbol="X", bars=full.loc[:now], now=now, market_open=False, quote=None, account=None, positions={}, open_orders=[])
    assert a.to_dict() == b.to_dict()
