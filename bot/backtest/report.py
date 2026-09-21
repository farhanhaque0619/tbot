"""Render backtest / walk-forward results to the console and to files."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.table import Table

from bot.backtest.engine import BacktestResult
from bot.backtest.metrics import format_metrics
from bot.backtest.walkforward import WalkForwardResult


def _json_safe(o: Any):
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return None
    return str(o)


def _params(p: dict[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in p.items())


def metrics_table(title: str, metrics: dict[str, Any], benchmark: dict[str, Any] | None = None) -> Table:
    t = Table(title=title, show_header=True, header_style="bold")
    t.add_column("Metric")
    t.add_column("Strategy", justify="right")
    if benchmark:
        t.add_column("Buy & hold", justify="right")
    rows = format_metrics(metrics)
    brow = dict(format_metrics(benchmark)) if benchmark else {}
    for k, v in rows:
        if benchmark:
            t.add_row(k, v, brow.get(k, "-") if k not in ("Trades", "Win rate", "Profit factor", "Avg trade P&L", "Net trade P&L", "Time in market") else "-")
        else:
            t.add_row(k, v)
    return t


def verdict(metrics: dict[str, Any], benchmark: dict[str, Any] | None) -> str:
    parts = []
    if metrics["total_return"] <= 0:
        parts.append("LOSES MONEY after costs")
    else:
        parts.append("positive after costs")
    if benchmark:
        if metrics["total_return"] < benchmark["total_return"]:
            parts.append(f"underperforms buy-and-hold ({metrics['total_return']:+.1%} vs {benchmark['total_return']:+.1%})")
        else:
            parts.append(f"beats buy-and-hold ({metrics['total_return']:+.1%} vs {benchmark['total_return']:+.1%})")
        if metrics["sharpe"] < benchmark["sharpe"]:
            parts.append(f"lower Sharpe ({metrics['sharpe']:.2f} vs {benchmark['sharpe']:.2f})")
        else:
            parts.append(f"higher Sharpe ({metrics['sharpe']:.2f} vs {benchmark['sharpe']:.2f})")
    if metrics["trade_count"] < 20:
        parts.append(f"only {metrics['trade_count']} trades - not statistically meaningful")
    return "; ".join(parts)


def print_backtest(res: BacktestResult, console: Console | None = None) -> None:
    console = console or Console()
    title = f"{res.strategy} {res.params} on {', '.join(res.symbols)}"
    console.print(metrics_table(title, res.metrics, res.benchmark_metrics))
    console.print(f"Costs paid: {res.costs_paid:,.2f}  ·  orders: {res.orders}  ·  daily-loss halts: {res.metrics.get('daily_halts', 0)}")
    for n in res.notes:
        console.print(f"[bold red]{n}[/bold red]")
    console.print(f"[bold]Verdict:[/bold] {verdict(res.metrics, res.benchmark_metrics)}")


def print_walkforward(wf: WalkForwardResult, console: Console | None = None) -> None:
    console = console or Console()
    ft = Table(title="Walk-forward folds (params chosen on train only)", header_style="bold")
    for c in ("Train", "Test", "Params", "Train Sharpe", "Test return", "Test Sharpe", "Test MaxDD", "Trades", "B&H return"):
        ft.add_column(c, justify="right" if c not in ("Train", "Test", "Params") else "left")
    for f in wf.folds:
        m, b = f.test_result.metrics, f.test_result.benchmark_metrics
        ft.add_row(f"{f.train_start.date()}→{f.train_end.date()}", f"{f.test_start.date()}→{f.test_end.date()}",
                   _params(f.best_params), f"{f.train_metrics['sharpe']:.2f}", f"{m['total_return']:+.1%}", f"{m['sharpe']:.2f}",
                   f"{m['max_drawdown']:.1%}", str(m["trade_count"]), f"{b['total_return']:+.1%}" if b else "-")
    console.print(ft)
    console.print(metrics_table(f"OUT-OF-SAMPLE (stitched) · {wf.strategy} on {', '.join(wf.symbols)}", wf.oos_metrics, wf.benchmark_metrics))
    for n in wf.notes:
        console.print(f"[dim]{n}[/dim]")
    console.print(f"[bold]OOS verdict:[/bold] {verdict(wf.oos_metrics, wf.benchmark_metrics)}")


def save_backtest(res: BacktestResult, out_dir: Path, tag: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    p = out_dir / f"{tag}_equity.csv"
    eq = pd.DataFrame({"equity": res.equity})
    if res.benchmark_equity is not None:
        eq["benchmark"] = res.benchmark_equity
    eq.to_csv(p); paths.append(p)
    p = out_dir / f"{tag}_trades.csv"
    res.trades_frame().to_csv(p, index=False); paths.append(p)
    p = out_dir / f"{tag}_metrics.json"
    p.write_text(json.dumps({"strategy": res.strategy, "params": res.params, "symbols": res.symbols,
                             "metrics": res.metrics, "benchmark": res.benchmark_metrics, "notes": res.notes},
                            indent=2, default=_json_safe)); paths.append(p)
    return paths


def save_walkforward(wf: WalkForwardResult, out_dir: Path, tag: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    p = out_dir / f"{tag}_wf_equity.csv"
    eq = pd.DataFrame({"equity": wf.oos_equity})
    if wf.benchmark_equity is not None:
        eq["benchmark"] = wf.benchmark_equity
    eq.to_csv(p); paths.append(p)
    p = out_dir / f"{tag}_wf_folds.json"
    p.write_text(json.dumps({
        "strategy": wf.strategy, "symbols": wf.symbols, "notes": wf.notes, "oos_metrics": wf.oos_metrics,
        "benchmark_metrics": wf.benchmark_metrics,
        "folds": [{"train": [f.train_start, f.train_end], "test": [f.test_start, f.test_end], "best_params": f.best_params,
                   "train_metrics": f.train_metrics, "test_metrics": f.test_result.metrics,
                   "test_benchmark": f.test_result.benchmark_metrics} for f in wf.folds]}, indent=2, default=_json_safe))
    paths.append(p)
    return paths
