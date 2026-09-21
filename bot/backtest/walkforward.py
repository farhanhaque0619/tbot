"""Walk-forward optimisation.

The history is cut into consecutive (train, test) windows. On each train window
every parameter set in ``Strategy.param_grid()`` is backtested and the best one
(by ``select_metric``, subject to ``min_trades``) is chosen *using train data
only*. That parameter set is then run once on the following test window. The
test windows are stitched (compounding daily returns) into a single
out-of-sample equity curve - the number that matters.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from bot.backtest.costs import CostModel
from bot.backtest.engine import BacktestResult, Backtester, Trade
from bot.backtest.metrics import compute_metrics
from bot.risk.manager import RiskLimits
from bot.strategies.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class Fold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict[str, Any]
    train_metrics: dict[str, Any]
    test_result: BacktestResult
    grid_results: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)


@dataclass
class WalkForwardResult:
    strategy: str
    symbols: list[str]
    folds: list[Fold]
    oos_equity: pd.Series
    oos_metrics: dict[str, Any]
    oos_trades: list[Trade]
    benchmark_equity: pd.Series | None
    benchmark_metrics: dict[str, Any] | None
    notes: list[str] = field(default_factory=list)


def _years(y: float) -> pd.DateOffset:
    return pd.DateOffset(years=int(y)) if float(y).is_integer() else pd.DateOffset(days=int(y * 365.25))


def _slice(data: dict[str, pd.DataFrame], start, end, warmup: int) -> dict[str, pd.DataFrame]:
    out = {}
    for s, df in data.items():
        before = df.loc[: start - pd.Timedelta(seconds=1)].tail(warmup)
        within = df.loc[start:end]
        if not within.empty:
            out[s] = pd.concat([before, within])
    return out


def walk_forward(strategy_cls: type[Strategy], data: dict[str, pd.DataFrame], *,
                 train_years: float = 3.0, test_years: float = 1.0, initial_cash: float = 100_000.0,
                 costs: CostModel | None = None, risk: RiskLimits | None = None,
                 select_metric: str = "sharpe", min_trades: int = 5,
                 start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
                 grid: list[dict[str, Any]] | None = None) -> WalkForwardResult:
    costs = costs or CostModel()
    risk = risk or RiskLimits()
    grid = grid or strategy_cls.param_grid()
    warmup = max(strategy_cls(**p).warmup for p in grid)
    all_idx = sorted(set().union(*[set(df.index) for df in data.values()]))
    first, last = all_idx[0], all_idx[-1]
    start = max(pd.Timestamp(start).tz_localize(first.tzinfo) if start is not None and pd.Timestamp(start).tzinfo is None else (start or first), first)
    end = min(pd.Timestamp(end).tz_localize(first.tzinfo) if end is not None and pd.Timestamp(end).tzinfo is None else (end or last), last)

    folds: list[Fold] = []
    train_len = _years(train_years)
    test_len = _years(test_years)
    t0 = start
    while True:
        train_start, train_end = t0, t0 + train_len - pd.Timedelta(days=1)
        test_start, test_end = t0 + train_len, min(t0 + train_len + test_len - pd.Timedelta(days=1), end)
        if test_start > end:
            break
        if (test_end - test_start).days < 30:  # skip a useless sliver at the end
            break
        train_data = _slice(data, train_start, train_end, warmup)
        test_data = _slice(data, test_start, test_end, warmup)
        if not train_data or not test_data:
            break
        # ---- grid search on train only ----
        scored: list[tuple[dict, dict]] = []
        for params in grid:
            bt = Backtester(lambda p=params: strategy_cls(**p), initial_cash=initial_cash, costs=costs, risk=risk,
                            trade_start=train_start, trade_end=train_end, benchmark=False)
            res = bt.run(train_data)
            scored.append((params, res.metrics))
        eligible = [(p, m) for p, m in scored if m["trade_count"] >= min_trades]
        pool = eligible or scored
        best_params, best_m = max(pool, key=lambda pm: pm[1][select_metric])
        # ---- one honest run on test ----
        bt = Backtester(lambda p=best_params: strategy_cls(**p), initial_cash=initial_cash, costs=costs, risk=risk,
                        trade_start=test_start, trade_end=test_end, benchmark=True)
        test_res = bt.run(test_data)
        log.info("fold %s..%s -> test %s..%s: best %s (train %s=%.2f) test %s=%.2f, trades=%d",
                 train_start.date(), train_end.date(), test_start.date(), test_end.date(), best_params,
                 select_metric, best_m[select_metric], select_metric, test_res.metrics[select_metric],
                 test_res.metrics["trade_count"])
        folds.append(Fold(train_start, train_end, test_start, test_end, best_params, best_m, test_res, scored))
        t0 = t0 + test_len

    if not folds:
        raise ValueError("not enough data for a single walk-forward fold "
                         f"(need > {train_years + test_years} years between {start.date()} and {end.date()})")

    # stitch OOS folds by compounding daily returns
    rets = pd.concat([f.test_result.equity.pct_change().fillna(0.0) for f in folds])
    rets = rets[~rets.index.duplicated(keep="first")]
    oos_equity = (1 + rets).cumprod() * initial_cash
    oos_trades = [t for f in folds for t in f.test_result.trades]
    exposure = sum(f.test_result.exposure * len(f.test_result.equity) for f in folds) / max(len(oos_equity), 1)
    oos_metrics = compute_metrics(oos_equity, oos_trades, exposure=exposure)
    bench = None
    bench_m = None
    if all(f.test_result.benchmark_equity is not None for f in folds):
        brets = pd.concat([f.test_result.benchmark_equity.pct_change().fillna(0.0) for f in folds])
        brets = brets[~brets.index.duplicated(keep="first")]
        bench = (1 + brets).cumprod() * initial_cash
        bench_m = compute_metrics(bench)
    notes = [f"{len(folds)} folds, train {train_years}y / test {test_years}y, selected by train {select_metric} (min {min_trades} trades)",
             "Folds are stitched by compounding daily returns; positions and risk state (peak equity, kill switch) "
             "reset at each fold boundary, and positions still open at a fold's end are closed at its last bar."]
    killed = [f for f in folds if f.test_result.killed]
    if killed:
        notes.append(f"Kill switch tripped in {len(killed)} test fold(s): " + ", ".join(str(f.test_start.date()) for f in killed))
    return WalkForwardResult(strategy_cls.name, list(data), folds, oos_equity, oos_metrics, oos_trades, bench, bench_m, notes)
