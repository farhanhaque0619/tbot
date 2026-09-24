import pandas as pd

from bot.backtest import Backtester, CostModel
from bot.risk import RiskLimits, RiskManager, RiskState, fixed_fractional_qty
from bot.strategies.base import Bar, Signal, Strategy
from tests.conftest import make_bars


def ts(d: str) -> pd.Timestamp:
    return pd.Timestamp(d, tz="America/New_York")


def test_kill_switch_trips_on_drawdown_and_stays_tripped():
    rm = RiskManager(RiskLimits(max_drawdown_pct=0.10, daily_loss_limit_pct=0.5))
    assert rm.update_equity(ts("2024-01-02"), 100_000)[0].kind == "new_day"
    assert [e.kind for e in rm.update_equity(ts("2024-01-03"), 95_000)] == ["new_day"]
    events = rm.update_equity(ts("2024-01-04"), 89_999)
    assert any(e.kind == "kill_switch" for e in events)
    assert rm.killed
    assert rm.can_open(0) == (False, "kill switch active")
    # recovery does not un-kill: a human has to reset
    rm.update_equity(ts("2024-01-05"), 120_000)
    assert rm.killed
    rm.reset_kill_switch()
    assert not rm.killed and rm.can_open(0)[0]


def test_daily_loss_limit_halts_then_resets_next_day():
    rm = RiskManager(RiskLimits(daily_loss_limit_pct=0.03, max_drawdown_pct=0.9))
    rm.update_equity(ts("2024-01-02 16:00"), 100_000)
    rm.update_equity(ts("2024-01-03 10:00"), 98_000)        # -2%: fine
    assert rm.can_open(0)[0]
    events = rm.update_equity(ts("2024-01-03 11:00"), 96_900)  # -3.1% vs day start 100k
    assert any(e.kind == "daily_halt" for e in events)
    assert not rm.can_open(0)[0] and rm.halted_today
    rm.update_equity(ts("2024-01-04 10:00"), 96_950)        # new day -> reference resets to 96,900
    assert not rm.halted_today and rm.can_open(0)[0]


def test_max_positions_cap():
    rm = RiskManager(RiskLimits(max_positions=2))
    rm.update_equity(ts("2024-01-02"), 100_000)
    assert rm.can_open(1)[0]
    ok, why = rm.can_open(2)
    assert not ok and "max positions" in why


def test_fixed_fractional_sizing():
    # risk 1% of 100k = $1,000; stop 2$ away -> 500 shares; notional 500*50 = 25k <= 50% cap
    assert fixed_fractional_qty(100_000, 50, 2.0, risk_pct=0.01, max_position_pct=0.5) == 500
    # notional cap binds: 1% of 100k / $0.10 stop = 10,000 shares = $500k -> capped to $50k = 1000 shares
    assert fixed_fractional_qty(100_000, 50, 0.10, risk_pct=0.01, max_position_pct=0.5) == 1000
    # cash cap binds
    assert fixed_fractional_qty(100_000, 50, 0.10, risk_pct=0.01, max_position_pct=0.5, cash_available=10_000) == 200
    assert fixed_fractional_qty(0, 50, 1, risk_pct=0.01, max_position_pct=0.5) == 0


def test_risk_state_roundtrip():
    s = RiskState(peak_equity=1, day="2024-01-02", killed=True, kill_reason="x")
    assert RiskState.from_dict(s.to_dict()) == s


class AlwaysLong(Strategy):
    """Goes long on the first bar. With far_stop=True it supplies a stop so far away that
    the fixed-fractional sizer fills the notional cap (i.e. a fully invested position)."""
    name = "always_long"
    default_params = {"far_stop": False}
    @property
    def warmup(self): return 0
    def reset(self): self.done = False
    def on_bar(self, bar: Bar):
        if not self.done:
            self.done = True
            return Signal(bar.symbol, 1, "in", stop_price=bar.close * 0.01 if self.params["far_stop"] else None)
        return None
    def on_position_closed(self, symbol, reason): self.done = False


def test_backtest_kill_switch_liquidates_and_stops_trading():
    """A steady crash must trip the kill switch: everything sold at the next open, nothing traded after."""
    n = 120
    closes = [100 * (0.99 ** i) for i in range(n)]  # -1% per day, no stop can save this fully-invested position
    df = make_bars(n, seed=7, closes=closes)
    risk = RiskLimits(risk_per_trade_pct=1.0, max_position_pct=1.0, max_drawdown_pct=0.15, daily_loss_limit_pct=0.5)
    res = Backtester(lambda: AlwaysLong(far_stop=True), costs=CostModel(0, 0, 0), risk=risk, benchmark=False).run({"X": df})
    assert res.killed
    kill = [e for e in res.risk_events if e.kind == "kill_switch"]
    assert len(kill) == 1
    kill_ts = kill[0].ts
    assert res.equity.loc[kill_ts] / res.equity.max() - 1 <= -0.15
    # exactly one trade, exited on the bar after the kill event, and flat forever after
    assert len(res.trades) == 1 and res.trades[0].exit_reason == "kill switch"
    assert res.trades[0].exit_ts == df.index[df.index.get_loc(kill_ts) + 1]
    after = res.equity.loc[res.trades[0].exit_ts:]
    assert (after == after.iloc[0]).all(), "equity must be flat after liquidation (no re-entry)"
    assert res.notes and "KILL SWITCH" in res.notes[0]


def test_backtest_protective_stop_exits_position():
    closes = [100.0] * 30 + [100 - 0.5 * i for i in range(1, 41)]  # slow bleed after a flat start
    df = make_bars(len(closes), seed=8, closes=closes)
    risk = RiskLimits(risk_per_trade_pct=0.01, max_position_pct=1.0, atr_stop_mult=2.0, max_drawdown_pct=0.9, daily_loss_limit_pct=0.9)
    res = Backtester(AlwaysLong, costs=CostModel(0, 0, 0), risk=risk, benchmark=False).run({"X": df})
    assert res.trades and res.trades[0].exit_reason.startswith("stop")
    t = res.trades[0]
    # loss is bounded near the 1% risk budget (gap through the stop can add a little)
    assert -0.03 * 100_000 < t.pnl < 0
