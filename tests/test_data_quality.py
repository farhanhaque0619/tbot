import numpy as np

from bot.data.quality import check_frame
from tests.conftest import make_bars


def test_quality_report_flags_gaps_splits_and_synthetic():
    df = make_bars(120, seed=3)
    df = df.drop(df.index[[10, 11]])                      # a gap
    for c in ("open", "high", "low", "close"):
        df.iloc[60:, df.columns.get_loc(c)] *= 0.5         # an unadjusted 2:1 split
    rep = check_frame("X", df, source="test")
    assert len(rep.missing_weekdays) == 2 and rep.suspicious_jumps and rep.ok
    df2 = df.copy()
    for c in ("open", "high", "low"):
        df2[c] = df2["close"]
    assert check_frame("X", df2).synthetic_ohlc
    bad = df.copy()
    bad.iloc[5, bad.columns.get_loc("low")] = bad["close"].max() * 2   # low above close
    r = check_frame("X", bad)
    assert r.ohlc_inconsistent == 1 and not r.ok


def test_backtest_survives_split_and_gap_in_data():
    """An unadjusted split shows up as a -50% bar. The engine must not crash, and the stop must exit."""
    from bot.backtest import Backtester, CostModel
    from bot.risk import RiskLimits
    from bot.strategies import MACrossover
    closes = 100 + np.arange(300) * 0.3
    closes[200:] = closes[200:] / 2                        # 2:1 split, unadjusted
    df = make_bars(300, seed=4, closes=closes)
    df = df.drop(df.index[[50, 51, 52]])                   # 3-day gap
    res = Backtester(lambda: MACrossover(fast=5, slow=20), costs=CostModel(0, 0), risk=RiskLimits(max_drawdown_pct=0.9, daily_loss_limit_pct=0.9),
                     benchmark=True).run({"X": df})
    assert any(t.exit_reason.startswith("stop") for t in res.trades)
    assert len(res.equity) == len(df)
