import numpy as np
import pandas as pd

from bot.strategies import MACrossover, MeanReversion, STRATEGIES, get_strategy_class
from bot.strategies.indicators import RollingATR, RollingMean, RollingStats, atr
from tests.conftest import make_bars


def test_registry():
    assert set(STRATEGIES) == {"ma_crossover", "mean_reversion"}
    assert get_strategy_class("ma_crossover") is MACrossover


def test_ma_crossover_goes_long_in_uptrend_and_flat_in_downtrend():
    closes = np.concatenate([np.linspace(100, 200, 150), np.linspace(200, 100, 150)])
    df = make_bars(300, seed=1, closes=closes)
    sig = MACrossover(fast=5, slow=20).generate_signals(df)
    assert sig.iloc[:19].eq(0).all()          # warm-up
    assert sig.iloc[30:140].eq(1).all()        # uptrend -> long
    assert sig.iloc[200:].eq(0).all()          # downtrend -> flat


def test_mean_reversion_buys_dips_and_exits_on_reversion():
    closes = np.array([100.0] * 40 + [90.0] + [100.0] * 10)   # one sharp dip then recovery
    df = make_bars(len(closes), seed=2, closes=closes)
    # constant closes -> std 0 -> no signal until the dip enters the window; use tiny noise instead
    closes = closes + np.random.default_rng(0).normal(0, 0.1, len(closes))
    df = make_bars(len(closes), seed=2, closes=closes)
    strat = MeanReversion(lookback=20, entry_z=2.0, exit_z=0.5)
    sig = strat.generate_signals(df)
    assert sig.iloc[40] == 1, "dip of ~10 sigma should trigger a long"
    assert sig.iloc[-1] == 0, "should exit after price reverts"


def test_rolling_indicators_match_pandas():
    df = make_bars(100, seed=3)
    rm = RollingMean(10)
    vals = [rm.update(x) for x in df["close"]]
    pd.testing.assert_series_equal(pd.Series(vals[9:], index=df.index[9:]), df["close"].rolling(10).mean().iloc[9:], check_names=False)
    rs = RollingStats(10)
    out = [rs.update(x) for x in df["close"]]
    stds = pd.Series([o[1] for o in out[9:]], index=df.index[9:])
    pd.testing.assert_series_equal(stds, df["close"].rolling(10).std(ddof=1).iloc[9:], check_names=False)
    ra = RollingATR(14)
    inc = [ra.update(h, l, c) for h, l, c in zip(df["high"], df["low"], df["close"])]
    vec = atr(df, 14)
    assert np.allclose(inc[13:], vec.iloc[13:], rtol=1e-9)


def test_unknown_param_rejected():
    import pytest
    with pytest.raises(ValueError):
        MACrossover(bogus=1)
    with pytest.raises(ValueError):
        MACrossover(fast=50, slow=20)
