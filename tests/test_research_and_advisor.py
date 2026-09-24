"""Phases 7/8/10/11/15: harness, surfaces, candidates, advisor shadow mode, Kelly research-only, review."""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from bot.backtest import CostModel
from bot.risk import RiskLimits
from bot.strategies import MACrossover, MeanReversion
from tests.conftest import make_bars

RISK = RiskLimits(max_drawdown_pct=0.9, daily_loss_limit_pct=0.9)


def test_compare_runs_all_cuts_and_regimes():
    from bot.research.harness import compare
    data = {"X": make_bars(1200, seed=21, drift=0.0004)}
    res = compare({"ma": lambda: MACrossover(fast=10, slow=50), "mr": lambda: MeanReversion(lookback=20)}, data,
                  costs=CostModel(), risk=RISK, walk_forward_classes={"ma": MACrossover}, train_years=2, test_years=1)
    cuts = {r["cut"] for r in res.rows}
    assert cuts == {"full", "in_sample", "validation", "held_out_test"}
    names = {r["strategy"] for r in res.rows}
    assert names == {"cash", "buy_and_hold", "ma", "mr"}
    cash = next(r for r in res.rows if r["strategy"] == "cash" and r["cut"] == "full")
    assert cash["total_return"] == 0 and cash["sharpe"] == 0
    bh = next(r for r in res.rows if r["strategy"] == "buy_and_hold" and r["cut"] == "full")
    assert bh["avg_gross_exposure"] > 0.95 and bh["trade_count"] == 1
    assert res.regime_rows is not None and set(res.regime_rows["regime"]) == {"bull", "bear", "sideways", "high_vol", "low_vol"}
    assert "ma" in res.walk_forward and res.walk_forward["ma"]["folds"] >= 1
    md = res.render_markdown()
    assert "held_out_test" in md and "walk-forward" in md and "Turnover/y" in md
    # cuts are chronological and disjoint
    c = {x.name: x for x in res.cuts}
    assert c["in_sample"].end < c["validation"].start < c["validation"].end < c["held_out_test"].start


def test_regime_labels_use_only_past_data():
    from bot.research.harness import classify_regimes
    close = make_bars(400, seed=22)["close"]
    a = classify_regimes(close)
    b = classify_regimes(pd.concat([close.iloc[:300], close.iloc[300:] * 0 + 1.0]))   # wreck the future
    # vol threshold is a full-sample quantile (documented), so compare the trend labels which are strictly causal
    pd.testing.assert_frame_equal(a[["bull", "bear", "sideways"]].iloc[:300], b[["bull", "bear", "sideways"]].iloc[:300])


def test_parameter_surface_reports_every_point_and_flags_fragility():
    from bot.research.surface import SurfaceResult, parameter_surface
    data = {"X": make_bars(900, seed=23)}
    sr = parameter_surface(MACrossover, data, costs=CostModel(), risk=RISK)
    assert len(sr.table) == len(MACrossover.param_grid())
    assert set(sr.best) == {"fast", "slow"} and isinstance(sr, SurfaceResult)
    assert "neighbour/best" in sr.render_markdown()


def test_candidates_run_and_are_research_only():
    from bot.backtest import Backtester
    from bot.research.candidates import RESEARCH_STRATEGIES
    data = {"X": make_bars(600, seed=24, drift=0.0005)}
    for name, cls in RESEARCH_STRATEGIES.items():
        res = Backtester(cls, costs=CostModel(), risk=RISK, benchmark=False).run(data)
        assert len(res.equity) == 600, name
        sig = cls().generate_signals(data["X"])
        assert set(sig.unique()) <= {0, 1}, name


def test_advisor_shadow_mode_has_zero_effect_on_orders(tmp_path):
    from bot.advisor.base import MarketAdvice
    from bot.config import Settings
    from bot.data.calendar import NY
    from bot.data.loader import BarLoader
    from bot.data.store import BarStore
    from bot.execution import FakeBroker, StateStore, Trader
    from bot.monitoring.decisions import DecisionLog

    closes = 100 + np.arange(300) * 0.2
    df = make_bars(300, seed=1, closes=closes)
    df.index = pd.bdate_range(end="2024-01-05", periods=300, tz=NY)

    class Contrarian:
        name = "contrarian"
        calls = 0

        def advise(self, state):
            Contrarian.calls += 1
            return MarketAdvice("crisis", 0, "short", "unsafe", "contrarian")

    def run(with_advisor: bool):
        store = BarStore(); store.upsert_bars("SPY", df); store.set_coverage("SPY", df.index[0].date(), df.index[-1].date())
        broker = FakeBroker(cash=100_000, prices={"SPY": float(df["close"].iloc[-1])})
        t = Trader(settings=Settings(_env_file=None, allow_fractional=False, log_dir=tmp_path), broker=broker, loader=BarLoader(store, None),
                   bar_store=store, strategy_cls=MACrossover, params={"fast": 10, "slow": 50}, symbols=["SPY"],
                   state_store=StateStore(tmp_path / f"{with_advisor}.json"), decision_log=DecisionLog(tmp_path / f"{with_advisor}.jsonl"))
        if with_advisor:
            from bot.advisor import ShadowRecorder
            t.advisor, t.shadow = Contrarian(), ShadowRecorder(tmp_path / "shadow.jsonl")
        t.run_cycle(datetime(2024, 1, 5, 19, 30, tzinfo=NY))
        return [(o.symbol, o.side, o.qty, o.client_order_id) for o in broker.submitted]

    assert run(False) == run(True) and Contrarian.calls == 1
    from bot.advisor import ShadowRecorder, calibration
    recs = ShadowRecorder(tmp_path / "shadow.jsonl").read()
    assert recs and recs[0]["advice"]["direction"] == "short" and recs[0]["strategy_signal"] == 1
    fut = pd.concat([df, pd.DataFrame({"open": 200.0, "high": 200.0, "low": 200.0, "close": 200.0, "volume": 1.0},
                                      index=pd.bdate_range("2024-01-08", periods=10, tz=NY))])
    cal = calibration(recs, {"SPY": fut}, horizon=5)
    row = cal[(cal["field"] == "direction") & (cal["value"] == "short")].iloc[0]
    assert row["n"] == 1 and row["hit_rate"] == 0.0 and row["mean_fwd_return"] > 0


def test_advice_validation_closes_the_vocabulary():
    from bot.advisor.base import MarketAdvice
    a = MarketAdvice.validated({"regime": "moon", "setup_quality": 9, "direction": "LONG", "risk_state": "fine"}, source="x", latency_ms=1)
    assert a.regime == "unclear" and a.setup_quality == 3 and a.direction == "neutral" and a.risk_state == "unsafe"


def test_jev_adapter_never_raises_and_is_off_by_default():
    from bot.advisor import build_advisor
    from bot.advisor.jev import JevAdvisor
    from bot.config import Settings
    from bot.execution.market_state import MarketState
    assert build_advisor(Settings(_env_file=None)) is None
    import dataclasses
    fields = [f.name for f in dataclasses.fields(MarketState)]
    vals = {"timestamp": "t", "symbol": "SPY", "market_open": True, "open_orders": 0, "last_bar_date": "2024-01-05", "n_bars": 300}
    st = MarketState(**{f: vals.get(f, 1.0) for f in fields})
    adv = JevAdvisor("", "")
    a = adv.advise(st)
    assert a.error and a.risk_state == "unsafe"

    class BadSession:
        def post(self, *a, **k):
            raise ConnectionError("down")
    a = JevAdvisor("https://example.invalid/x", "k", session=BadSession()).advise(st)
    assert a.error == "ConnectionError" and a.direction == "neutral"


def test_kelly_is_research_only_and_bounded():
    from bot.research.kelly import fractional_kelly, kelly_fraction, kelly_from_trades
    assert kelly_fraction(0.5, 1.0) == 0.0 and kelly_fraction(0.6, 1.0) == pytest.approx(0.2)
    assert fractional_kelly(0.9, 10.0) == 0.10       # capped
    assert kelly_from_trades([1.0] * 10)["fractional"] == 0.0


def test_review_report_is_read_only(tmp_path):
    from bot.config import Settings
    from bot.execution.state import BotState, StateStore
    from bot.research.review import build_review
    s = Settings(_env_file=None, state_dir=tmp_path, log_dir=tmp_path)
    st = BotState(run_id="paper", env="paper", strategy="ma_crossover", risk={"killed": True, "kill_reason": "test"},
                  equity_log=[{"ts": "2024-01-05T10:00", "equity": 100.0, "cash": 100.0}, {"ts": "2024-01-05T11:00", "equity": 99.0, "cash": 99.0}])
    StateStore(tmp_path / "paper.json").save(st)
    (tmp_path / "decisions_paper.jsonl").write_text('{"order_decision": "blocked", "risk_decision": {"code": "data_fresh"}, "signal": {"target": 1}, "desired_position": 1, "current_broker_position": 0}\n{"order_decision": "fill", "realized_slippage_bps": 12.0}\n')
    before = (tmp_path / "paper.json").read_text()
    md = build_review(s, "paper")
    assert "ACTION REQUIRED" in md and "Proposals" in md and "data_fresh" in md and "changed nothing" in md
    assert (tmp_path / "paper.json").read_text() == before
