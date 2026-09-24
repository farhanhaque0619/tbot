"""Research harness (Phase 7): compare strategies against cash and buy-and-hold on several sample cuts and
market regimes, using the SAME backtester, costs and risk settings as execution. Never touches a broker."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from bot.backtest.costs import CostModel
from bot.backtest.engine import Backtester, BacktestResult
from bot.backtest.metrics import TRADING_DAYS, sharpe
from bot.backtest.walkforward import walk_forward
from bot.research.metrics_ext import METRIC_COLUMNS, extended_metrics, fmt
from bot.risk.manager import RiskLimits
from bot.strategies.base import Strategy

Factory = Callable[[], Strategy]


@dataclass
class Cut:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp


def sample_cuts(index: pd.DatetimeIndex, *, is_frac: float = 0.6, val_frac: float = 0.2) -> list[Cut]:
    """Full, in-sample (first 60%), validation (next 20%), held-out test (last 20%). Contiguous, chronological."""
    n = len(index)
    a, b = int(n * is_frac), int(n * (is_frac + val_frac))
    return [Cut("full", index[0], index[-1]), Cut("in_sample", index[0], index[a - 1]),
            Cut("validation", index[a], index[b - 1]), Cut("held_out_test", index[b], index[-1])]


def classify_regimes(benchmark_close: pd.Series, *, trend_window: int = 126, trend_thresh: float = 0.10,
                     vol_window: int = 21, vol_pct: float = 0.75) -> pd.DataFrame:
    """Deterministic regime labels from the benchmark's *past* returns (no lookahead: rolling windows end at t).
    bull: trailing 6-month return > +10%; bear: < -10%; sideways: in between. high_vol: trailing 1-month realised
    vol above the sample's 75th percentile."""
    r = benchmark_close.pct_change()
    trail = benchmark_close / benchmark_close.shift(trend_window) - 1
    vol = r.rolling(vol_window).std() * np.sqrt(TRADING_DAYS)
    thresh = vol.quantile(vol_pct)
    out = pd.DataFrame(index=benchmark_close.index)
    out["bull"] = trail > trend_thresh
    out["bear"] = trail < -trend_thresh
    out["sideways"] = trail.abs() <= trend_thresh
    out["high_vol"] = vol > thresh
    out["low_vol"] = vol <= thresh
    return out.fillna(False)


def regime_table(daily_returns: dict[str, pd.Series], regimes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for reg in ("bull", "bear", "sideways", "high_vol", "low_vol"):
        mask = regimes[reg]
        for name, r in daily_returns.items():
            rr = r.reindex(mask.index).fillna(0.0)[mask.values]
            rows.append({"regime": reg, "strategy": name, "days": int(len(rr)), "ann_return": float(rr.mean() * TRADING_DAYS),
                         "sharpe": sharpe(rr), "hit_rate": float((rr > 0).mean()) if len(rr) else 0.0,
                         "worst_day": float(rr.min()) if len(rr) else 0.0})
    return pd.DataFrame(rows)


def _buy_and_hold(data: dict[str, pd.DataFrame], cut: Cut, costs: CostModel, cash: float) -> BacktestResult:
    """Equal-weight buy-and-hold as a real BacktestResult (one entry per symbol at the first open, exit at the end)."""
    from bot.strategies.base import Bar, Signal

    class BuyHold(Strategy):
        name = "buy_and_hold"
        default_params: dict = {}

        @property
        def warmup(self): return 0
        def reset(self): pass
        def on_bar(self, bar: Bar):
            # Re-emit every bar: warm-up bars are not tradeable and the engine ignores a redundant target.
            return Signal(bar.symbol, 1, "buy and hold", stop_price=bar.close * 1e-6)
    n = len(data)
    risk = RiskLimits(risk_per_trade_pct=1.0, max_position_pct=1.0 / n, max_positions=n, daily_loss_limit_pct=1.0, max_drawdown_pct=1.0,
                      allow_fractional=True)
    return Backtester(BuyHold, initial_cash=cash, costs=costs, risk=risk, trade_start=cut.start, trade_end=cut.end, benchmark=False).run(data)


def _cash(data: dict[str, pd.DataFrame], cut: Cut, cash: float) -> BacktestResult:
    idx = sorted(set().union(*[set(df.loc[cut.start:cut.end].index) for df in data.values()]))
    eq = pd.Series(cash, index=pd.DatetimeIndex(idx), name="equity")
    return BacktestResult("cash", {}, list(data), eq, eq.copy(), [], extended_metrics(eq), gross_exposure=eq * 0)


@dataclass
class CompareResult:
    cuts: list[Cut]
    rows: list[dict[str, Any]] = field(default_factory=list)          # one per (cut, strategy)
    regimes: pd.DataFrame | None = None
    regime_rows: pd.DataFrame | None = None
    walk_forward: dict[str, Any] = field(default_factory=dict)          # strategy -> oos metrics
    notes: list[str] = field(default_factory=list)

    def table(self, cut: str) -> pd.DataFrame:
        rows = [r for r in self.rows if r["cut"] == cut]
        cols = ["strategy"] + [c for c, _, _ in METRIC_COLUMNS]
        return pd.DataFrame(rows)[cols] if rows else pd.DataFrame()

    def render_markdown(self) -> str:
        out = []
        for cut in self.cuts:
            rows = [r for r in self.rows if r["cut"] == cut.name]
            if not rows:
                continue
            out.append(f"\n### {cut.name}  ({cut.start.date()} → {cut.end.date()})\n")
            heads = ["Strategy"] + [h for _, h, _ in METRIC_COLUMNS]
            out.append("| " + " | ".join(heads) + " |")
            out.append("|" + "---|" * len(heads))
            for r in rows:
                out.append("| " + " | ".join([r["strategy"]] + [fmt(r.get(c), k) for c, _, k in METRIC_COLUMNS]) + " |")
        if self.walk_forward:
            out.append("\n### walk-forward (out-of-sample, stitched)\n")
            heads = ["Strategy", "Total ret", "CAGR", "Sharpe", "MaxDD", "Trades", "B&H ret", "B&H Sharpe", "folds"]
            out.append("| " + " | ".join(heads) + " |")
            out.append("|" + "---|" * len(heads))
            for name, m in self.walk_forward.items():
                out.append(f"| {name} | {fmt(m['total_return'], 'pct')} | {fmt(m['cagr'], 'pct')} | {fmt(m['sharpe'], 'num')} | {fmt(m['max_drawdown'], 'pct')} | "
                           f"{m['trade_count']} | {fmt(m.get('bench_total_return'), 'pct')} | {fmt(m.get('bench_sharpe'), 'num')} | {m.get('folds')} |")
        if self.regime_rows is not None and len(self.regime_rows):
            out.append("\n### regimes (daily returns of the full-sample run, benchmark-defined regimes)\n")
            out.append("| Regime | Strategy | Days | Ann. return | Sharpe | Hit rate | Worst day |")
            out.append("|---|---|---|---|---|---|---|")
            for _, r in self.regime_rows.iterrows():
                out.append(f"| {r['regime']} | {r['strategy']} | {int(r['days'])} | {fmt(r['ann_return'], 'pct')} | {fmt(r['sharpe'], 'num')} | {fmt(r['hit_rate'], 'pct')} | {fmt(r['worst_day'], 'pct')} |")
        for n in self.notes:
            out.append(f"\n_{n}_")
        return "\n".join(out)


def compare(strategies: dict[str, Factory], data: dict[str, pd.DataFrame], *, costs: CostModel, risk: RiskLimits,
            cash: float = 100_000.0, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
            walk_forward_classes: dict[str, type[Strategy]] | None = None, train_years: float = 3.0, test_years: float = 1.0) -> CompareResult:
    idx = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in data.values()])))
    if start is not None:
        idx = idx[idx >= start]
    if end is not None:
        idx = idx[idx <= end]
    cuts = sample_cuts(idx)
    res = CompareResult(cuts)
    daily: dict[str, pd.Series] = {}
    bench_full = None
    for cut in cuts:
        runs: dict[str, BacktestResult] = {"cash": _cash(data, cut, cash), "buy_and_hold": _buy_and_hold(data, cut, costs, cash)}
        for name, factory in strategies.items():
            runs[name] = Backtester(factory, initial_cash=cash, costs=costs, risk=risk, trade_start=cut.start, trade_end=cut.end, benchmark=False).run(data)
        for name, r in runs.items():
            m = extended_metrics(r.equity, r.trades, gross_exposure=r.gross_exposure, costs_paid=r.costs_paid,
                                 traded_notional=r.traded_notional, exposure=r.exposure)
            m.update(cut=cut.name, strategy=name, killed=r.killed)
            res.rows.append(m)
            if cut.name == "full":
                daily[name] = r.equity.pct_change().fillna(0.0)
                if name == "buy_and_hold":
                    bench_full = r.equity
    if bench_full is not None:
        res.regimes = classify_regimes(bench_full)
        res.regime_rows = regime_table(daily, res.regimes)
    for name, cls in (walk_forward_classes or {}).items():
        try:
            wf = walk_forward(cls, data, train_years=train_years, test_years=test_years, initial_cash=cash, costs=costs, risk=risk,
                              start=idx[0], end=idx[-1])
            m = dict(wf.oos_metrics)
            m["folds"] = len(wf.folds)
            if wf.benchmark_metrics:
                m["bench_total_return"] = wf.benchmark_metrics["total_return"]
                m["bench_sharpe"] = wf.benchmark_metrics["sharpe"]
            res.walk_forward[name] = m
        except ValueError as e:
            res.notes.append(f"walk-forward skipped for {name}: {e}")
    res.notes.append("Same engine, costs and risk limits as execution. Strategy sizing is fixed-fractional (partial exposure); "
                     "buy-and-hold is fully invested. Compare Sharpe/drawdown/exposure, not raw return alone.")
    res.notes.append("Regimes are labelled from the benchmark's trailing 6-month return and trailing 1-month volatility, so labels use only past data.")
    return res
