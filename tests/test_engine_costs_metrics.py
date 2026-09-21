import math

import numpy as np
import pandas as pd
import pytest

from bot.backtest import Backtester, CostModel, compute_metrics
from bot.backtest.metrics import max_drawdown, sharpe
from bot.risk import RiskLimits
from bot.strategies.base import Bar, Signal, Strategy
from tests.conftest import make_bars


class OneRoundTrip(Strategy):
    name = "one_round_trip"
    default_params = {"entry": 5, "exit": 15}
    @property
    def warmup(self): return 0
    def reset(self): self.i = -1
    def on_bar(self, bar: Bar):
        self.i += 1
        if self.i == self.params["entry"]: return Signal(bar.symbol, 1)
        if self.i == self.params["exit"]: return Signal(bar.symbol, 0)
        return None


def test_costs_are_applied_per_side_and_pnl_is_exact():
    df = make_bars(30, seed=11)
    costs = CostModel(slippage_bps=10, spread_bps=10, commission_per_share=0.01)   # 15 bps per side
    risk = RiskLimits(risk_per_trade_pct=0.01, max_position_pct=1.0, atr_stop_mult=1000, max_drawdown_pct=0.9, daily_loss_limit_pct=0.9)
    res = Backtester(OneRoundTrip, costs=costs, risk=risk, benchmark=False).run({"X": df})
    assert len(res.trades) == 1
    t = res.trades[0]
    o_in, o_out = df["open"].iloc[6], df["open"].iloc[16]
    assert t.entry_price == pytest.approx(o_in * (1 + 0.0015))
    assert t.exit_price == pytest.approx(o_out * (1 - 0.0015))
    expected = (t.exit_price - t.entry_price) * t.qty - 2 * 0.01 * t.qty
    assert t.pnl == pytest.approx(expected)
    # final equity = initial + pnl, and the equity curve reflects cash + marks
    assert res.equity.iloc[-1] == pytest.approx(100_000 + t.pnl)


def test_no_leverage_qty_is_capped_by_cash():
    df = make_bars(30, seed=12)
    risk = RiskLimits(risk_per_trade_pct=0.1, max_position_pct=1.0, atr_stop_mult=0.01, max_drawdown_pct=0.9, daily_loss_limit_pct=0.9)
    res = Backtester(OneRoundTrip, costs=CostModel(0, 0), risk=risk, benchmark=False).run({"X": df})
    t = res.trades[0]
    assert t.qty * t.entry_price <= 100_000
    assert res.cash.min() >= 0


def test_metrics_basic():
    idx = pd.bdate_range("2020-01-01", periods=253, tz="America/New_York")
    eq = pd.Series(np.linspace(100, 110, 253), index=idx)
    m = compute_metrics(eq)
    assert m["total_return"] == pytest.approx(0.10)
    assert m["max_drawdown"] == 0
    assert m["cagr"] == pytest.approx(0.10, rel=0.05)
    dd_eq = pd.Series([100, 120, 90, 95, 130], index=idx[:5])
    mdd, peak, trough = max_drawdown(dd_eq)
    assert mdd == pytest.approx(-0.25) and peak == idx[1] and trough == idx[2]
    assert sharpe(pd.Series([0.0, 0.0, 0.0])) == 0.0
    m2 = compute_metrics(eq, trades=[type("T", (), {"pnl": 10})(), type("T", (), {"pnl": -5})()])
    assert m2["win_rate"] == 0.5 and m2["profit_factor"] == 2.0 and m2["trade_count"] == 2


def test_multi_symbol_respects_max_positions():
    dfs = {f"S{i}": make_bars(30, seed=20 + i) for i in range(4)}
    risk = RiskLimits(risk_per_trade_pct=0.01, max_position_pct=0.2, atr_stop_mult=1000, max_positions=2, max_drawdown_pct=0.9, daily_loss_limit_pct=0.9)
    res = Backtester(OneRoundTrip, costs=CostModel(0, 0), risk=risk, benchmark=True).run(dfs)
    assert len(res.trades) == 2
    assert res.benchmark_equity is not None and len(res.benchmark_equity) == len(res.equity)


def test_open_position_is_closed_at_end_of_run_and_equity_agrees():
    """A position still open on the last bar is closed at that bar's close (with exit costs),
    so final equity == initial + sum(trade pnl) and the equity curve never jumps."""
    df = make_bars(30, seed=13)
    costs = CostModel(slippage_bps=10, spread_bps=10)
    risk = RiskLimits(risk_per_trade_pct=0.01, max_position_pct=1.0, atr_stop_mult=1000, max_drawdown_pct=0.9, daily_loss_limit_pct=0.9)
    res = Backtester(lambda: OneRoundTrip(entry=5, exit=999), costs=costs, risk=risk, benchmark=False).run({"X": df})
    assert len(res.trades) == 1 and res.trades[0].exit_reason == "end of backtest"
    t = res.trades[0]
    assert t.exit_ts == df.index[-1]
    assert t.exit_price == pytest.approx(df["close"].iloc[-1] * (1 - 0.0015))
    assert res.equity.iloc[-1] == pytest.approx(100_000 + t.pnl)
    # the liquidation only costs the exit slippage: last two equity marks differ by less than 0.5%
    assert abs(res.equity.iloc[-1] / res.equity.iloc[-2] - 1) < 0.005 + abs(df["close"].iloc[-1] / df["close"].iloc[-2] - 1)
