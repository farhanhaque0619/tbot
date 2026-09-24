"""Parameter surfaces (Phase 8): report every grid point, not only the best, and flag instability."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from bot.backtest.costs import CostModel
from bot.backtest.engine import Backtester
from bot.risk.manager import RiskLimits
from bot.strategies.base import Strategy


@dataclass
class SurfaceResult:
    strategy: str
    metric: str
    table: pd.DataFrame
    best: dict[str, Any]
    neighbour_ratio: float          # mean neighbour metric / best metric (1 = flat surface, <0.5 = fragile)
    fragile: bool
    notes: list[str] = field(default_factory=list)

    def render_markdown(self) -> str:
        cols = [c for c in self.table.columns]
        out = [f"| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for _, r in self.table.iterrows():
            out.append("| " + " | ".join(f"{v:.3f}" if isinstance(v, float) else str(v) for v in r.values) + " |")
        out.append(f"\nbest: {self.best} · neighbour/best {self.metric} ratio: {self.neighbour_ratio:.2f} · "
                   f"{'**FRAGILE** (profitability depends on the exact parameters)' if self.fragile else 'stable neighbourhood'}")
        return "\n".join(out)


def _neighbours(params: dict[str, Any], grid: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grid points that differ from ``params`` in exactly one parameter by one step."""
    out = []
    for key in params:
        values = sorted({g[key] for g in grid if all(g[k] == params[k] for k in params if k != key)}, key=lambda v: (str(type(v)), v))
        if params[key] not in values:
            continue
        i = values.index(params[key])
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                cand = dict(params)
                cand[key] = values[j]
                if cand in grid:
                    out.append(cand)
    return out


def parameter_surface(strategy_cls: type[Strategy], data: dict[str, pd.DataFrame], *, costs: CostModel, risk: RiskLimits,
                      cash: float = 100_000.0, start=None, end=None, metric: str = "sharpe", grid: list[dict[str, Any]] | None = None,
                      min_trades: int = 5) -> SurfaceResult:
    grid = grid or strategy_cls.param_grid()
    rows = []
    for params in grid:
        r = Backtester(lambda p=params: strategy_cls(**p), initial_cash=cash, costs=costs, risk=risk, trade_start=start, trade_end=end, benchmark=False).run(data)
        m = r.metrics
        rows.append({**params, "sharpe": m["sharpe"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"], "trades": m["trade_count"],
                     "profit_factor": min(m["profit_factor"], 99.0), "exposure": m.get("exposure", 0.0), "killed": r.killed})
    table = pd.DataFrame(rows)
    eligible = table[table["trades"] >= min_trades] if (table["trades"] >= min_trades).any() else table
    best_row = eligible.sort_values(metric, ascending=False).iloc[0]
    best = {k: best_row[k] for k in grid[0]}
    best = {k: (int(v) if isinstance(v, (np.integer,)) else (float(v) if isinstance(v, np.floating) else v)) for k, v in best.items()}
    neigh = _neighbours(best, grid)
    if neigh:
        vals = [float(table[(table[list(best)] == pd.Series(n)).all(axis=1)][metric].iloc[0]) for n in neigh]
        bm = float(best_row[metric])
        ratio = float(np.mean(vals) / bm) if bm > 0 else float("nan")
    else:
        ratio = float("nan")
    fragile = (not np.isnan(ratio)) and ratio < 0.5
    notes = [f"grid of {len(grid)} points; best chosen by {metric} among points with >= {min_trades} trades",
             f"positive-{metric} share of the grid: {(table[metric] > 0).mean():.0%}"]
    return SurfaceResult(strategy_cls.name, metric, table, best, ratio, fragile, notes)
