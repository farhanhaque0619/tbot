"""The V1.5 engine in daily-legacy mode must reproduce bot/backtest/engine.py exactly (Phase 0 frozen baselines are the oracle)."""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot.backtest import Backtester, CostModel
from bot.backtest.engine_v15 import run_daily_legacy
from bot.backtest.fills import FillParams
from bot.risk import RiskLimits
from bot.strategies import MACrossover, MeanReversion
from tests.conftest import make_bars

CACHE = Path("data_cache/bars.duckdb")


def _compare(factory, data, start=None, end=None):
    v1 = Backtester(factory, costs=CostModel(2, 2, 0), risk=RiskLimits(allow_fractional=True), trade_start=start, trade_end=end).run(data)
    v15 = run_daily_legacy(factory, data, fills=FillParams(slippage_bps=2, legacy_spread_bps=2), trade_start=start, trade_end=end)
    assert len(v1.equity) == len(v15.equity)
    rel = np.abs(v1.equity.to_numpy() - v15.equity.reindex(v1.equity.index).to_numpy()) / v1.equity.to_numpy()
    assert rel.max() < 1e-9, f"equity diverges: max rel diff {rel.max():.2e}"
    key = lambda t: (t.entry_ts, t.exit_ts, round(t.qty, 6), round(t.pnl, 6), t.exit_reason)  # noqa: E731
    assert [key(t) for t in v1.trades] == [key(t) for t in v15.trades]
    for m in ("total_return", "sharpe", "max_drawdown"):
        assert v1.metrics[m] == pytest.approx(v15.metrics[m], rel=1e-9, abs=1e-12), m
    return v1, v15


@pytest.mark.parametrize("factory", [lambda: MACrossover(fast=5, slow=20), lambda: MeanReversion(lookback=10), MACrossover, MeanReversion])
@pytest.mark.parametrize("seed", [1, 7])
def test_synthetic_daily_reproduction(factory, seed):
    _compare(factory, {"X": make_bars(600, seed=seed)})


def test_two_symbols_share_cash_like_v1():
    data = {"X": make_bars(400, seed=3), "Y": make_bars(400, seed=4, drift=-0.0002)}
    _compare(lambda: MACrossover(fast=5, slow=20), data)


def test_result_as_v1_is_a_backtest_result():
    from bot.backtest.engine import BacktestResult
    res = run_daily_legacy(lambda: MACrossover(fast=5, slow=20), {"X": make_bars(300, seed=2)})
    assert isinstance(res.as_v1(), BacktestResult) and res.strategy == "ma_crossover" and res.metrics["orders"] >= 0


@pytest.mark.skipif(not CACHE.exists(), reason="needs the cached SP500 proxy series (python -m bot data import)")
def test_frozen_sp500_proxy_baseline_is_reproduced():
    from bot.data.loader import BarLoader
    from bot.data.store import BarStore
    store = BarStore(str(CACHE))
    if "SP500" not in store.symbols():
        pytest.skip("SP500 proxy not cached")
    sp = {"SP500": BarLoader(store, None).get_daily("SP500", date(2000, 1, 3), date(2022, 12, 28), warmup=221)}
    t0 = pd.Timestamp("2000-01-03", tz="America/New_York")
    t1 = pd.Timestamp("2022-12-28", tz="America/New_York") + pd.Timedelta(hours=23)
    v1, v15 = _compare(MACrossover, sp, t0, t1)
    # frozen Phase 0 numbers (V1_5_AUDIT.md §2.3)
    assert v15.metrics["total_return"] == pytest.approx(0.957224, abs=5e-5)
    assert v15.metrics["sharpe"] == pytest.approx(0.5073, abs=5e-4)
    assert len(v15.trades) == 26
    _compare(MeanReversion, sp, t0, t1)
