"""Extended performance metrics for the research harness (Phase 7)."""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

from bot.backtest.metrics import compute_metrics


def time_underwater(equity: pd.Series) -> tuple[float, int]:
    """(fraction of bars below the running peak, longest underwater stretch in bars)."""
    if equity.empty:
        return 0.0, 0
    under = equity < equity.cummax()
    longest, cur = 0, 0
    for u in under:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    return float(under.mean()), int(longest)


def extended_metrics(equity: pd.Series, trades: Sequence[Any] = (), *, gross_exposure: pd.Series | None = None,
                     costs_paid: float = 0.0, traded_notional: float = 0.0, exposure: float | None = None) -> dict[str, Any]:
    m = compute_metrics(equity, trades, exposure=exposure)
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9) if len(equity) > 1 else 1e-9
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    avg_win, avg_loss = (float(wins.mean()) if len(wins) else 0.0), (float(losses.mean()) if len(losses) else 0.0)
    tu_frac, tu_longest = time_underwater(equity)
    avg_eq = float(equity.mean()) if len(equity) else 0.0
    holding = [t.bars_held for t in trades]
    m.update({
        "time_underwater_frac": tu_frac,
        "longest_underwater_bars": tu_longest,
        "hit_rate": m["win_rate"],
        "payoff_ratio": (avg_win / abs(avg_loss)) if avg_loss else (math.inf if avg_win > 0 else 0.0),
        "turnover_per_year": (traded_notional / avg_eq / years) if avg_eq > 0 else 0.0,
        "avg_holding_bars": float(np.mean(holding)) if holding else 0.0,
        "median_holding_bars": float(np.median(holding)) if holding else 0.0,
        "avg_gross_exposure": float((gross_exposure / equity).mean()) if gross_exposure is not None and len(gross_exposure) else (exposure or 0.0),
        "avg_net_exposure": float((gross_exposure / equity).mean()) if gross_exposure is not None and len(gross_exposure) else (exposure or 0.0),  # long-only baselines: net == gross
        "avg_slippage_bps": (costs_paid / traded_notional * 1e4) if traded_notional > 0 else 0.0,
        "cost_drag_annual": (costs_paid / equity.iloc[0] / years) if len(equity) else 0.0,
        "tail_loss_5pct_day": float(np.percentile(rets, 5)) if len(rets) > 20 else 0.0,
        "worst_day": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(pnls.max()) if len(pnls) else 0.0,
        "worst_trade": float(pnls.min()) if len(pnls) else 0.0,
        "trades_per_year": len(pnls) / years,
    })
    return m


METRIC_COLUMNS = [
    ("total_return", "Total ret", "pct"), ("cagr", "CAGR", "pct"), ("volatility", "Vol", "pct"), ("sharpe", "Sharpe", "num"),
    ("sortino", "Sortino", "num"), ("calmar", "Calmar", "num"), ("max_drawdown", "MaxDD", "pct"), ("time_underwater_frac", "Underwater", "pct"),
    ("longest_underwater_bars", "Longest UW", "int"), ("hit_rate", "Hit", "pct"), ("avg_win", "Avg win", "money"), ("avg_loss", "Avg loss", "money"),
    ("payoff_ratio", "Payoff", "num"), ("profit_factor", "PF", "num"), ("turnover_per_year", "Turnover/y", "num"),
    ("avg_holding_bars", "Hold(bars)", "num"), ("trade_count", "Trades", "int"), ("avg_gross_exposure", "Gross exp", "pct"),
    ("avg_net_exposure", "Net exp", "pct"), ("avg_slippage_bps", "Slip bps", "num"), ("cost_drag_annual", "Cost drag/y", "pct"),
    ("tail_loss_5pct_day", "5% tail day", "pct"), ("worst_day", "Worst day", "pct"), ("best_trade", "Best trade", "money"), ("worst_trade", "Worst trade", "money"),
]


def fmt(v: Any, kind: str) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    if kind == "pct":
        return f"{v:+.1%}" if abs(v) < 10 else f"{v:+.0%}"
    if kind == "num":
        return f"{v:.2f}"
    if kind == "int":
        return str(int(v))
    if kind == "money":
        return f"{v:,.0f}"
    return str(v)
