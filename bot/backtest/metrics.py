"""Performance metrics from an equity curve and a trade list."""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp | None, pd.Timestamp | None]:
    if equity.empty:
        return 0.0, None, None
    peak = equity.cummax()
    dd = equity / peak - 1
    trough = dd.idxmin()
    mdd = float(dd.min())
    peak_ts = equity.loc[:trough].idxmax() if mdd < 0 else None
    return mdd, peak_ts, (trough if mdd < 0 else None)


def sharpe(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * math.sqrt(periods))


def sortino(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    down = r[r < 0]
    if len(r) < 2 or len(down) == 0 or down.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / down.std(ddof=1) * math.sqrt(periods))


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    ratio = equity.iloc[-1] / equity.iloc[0]
    return float(ratio ** (1 / years) - 1) if ratio > 0 else -1.0


def compute_metrics(equity: pd.Series, trades: Sequence[Any] = (), *, exposure: float | None = None) -> dict[str, Any]:
    equity = equity.dropna()
    rets = equity.pct_change().dropna()
    mdd, peak_ts, trough_ts = max_drawdown(equity)
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0
    m: dict[str, Any] = {
        "start": equity.index[0] if len(equity) else None,
        "end": equity.index[-1] if len(equity) else None,
        "days": int(len(equity)),
        "initial_equity": float(equity.iloc[0]) if len(equity) else 0.0,
        "final_equity": float(equity.iloc[-1]) if len(equity) else 0.0,
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) else 0.0,
        "cagr": cagr(equity),
        "sharpe": sharpe(rets),
        "sortino": sortino(rets),
        "volatility": float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets) > 1 else 0.0,
        "max_drawdown": mdd,
        "max_drawdown_peak": peak_ts,
        "max_drawdown_trough": trough_ts,
        "trade_count": int(len(pnls)),
        "win_rate": float((pnls > 0).mean()) if len(pnls) else 0.0,
        "profit_factor": profit_factor,
        "avg_trade_pnl": float(pnls.mean()) if len(pnls) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "net_profit": float(pnls.sum()),
    }
    if exposure is not None:
        m["exposure"] = float(exposure)
    m["calmar"] = float(m["cagr"] / abs(mdd)) if mdd < 0 else 0.0
    return m


def format_metrics(m: dict[str, Any]) -> list[tuple[str, str]]:
    def pct(x): return f"{x:+.2%}"
    def num(x): return "inf" if x == math.inf else f"{x:.2f}"
    rows = [
        ("Period", f"{pd.Timestamp(m['start']).date() if m['start'] is not None else '-'} → {pd.Timestamp(m['end']).date() if m['end'] is not None else '-'} ({m['days']} bars)"),
        ("Final equity", f"{m['final_equity']:,.2f} (from {m['initial_equity']:,.2f})"),
        ("Total return", pct(m["total_return"])),
        ("CAGR", pct(m["cagr"])),
        ("Sharpe", num(m["sharpe"])),
        ("Sortino", num(m["sortino"])),
        ("Ann. volatility", pct(m["volatility"])),
        ("Max drawdown", pct(m["max_drawdown"])),
        ("Calmar", num(m["calmar"])),
        ("Trades", str(m["trade_count"])),
        ("Win rate", pct(m["win_rate"]) if m["trade_count"] else "-"),
        ("Profit factor", num(m["profit_factor"]) if m["trade_count"] else "-"),
        ("Avg trade P&L", f"{m['avg_trade_pnl']:,.2f}" if m["trade_count"] else "-"),
        ("Net trade P&L", f"{m['net_profit']:,.2f}"),
    ]
    if "exposure" in m:
        rows.append(("Time in market", pct(m["exposure"])))
    return rows
