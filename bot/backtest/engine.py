"""Event-driven backtester with strictly no lookahead.

Timeline for each bar ``t`` (all symbols aligned on the date grid):

1. **Fill** orders that were queued after bar ``t-1`` at bar ``t``'s *open*
   (plus slippage and half-spread). Signals never fill on the bar that produced them.
2. **Mark to market** at ``t``'s close, record equity, feed the risk manager
   (daily-loss halt, drawdown kill switch).
3. **Protective stops**: a position whose close breached its stop is queued to exit
   at ``t+1``'s open (stops are checked on completed bars, same as the paper loop).
4. **Strategy**: ``on_bar(bar_t)`` sees only bars ``<= t``; its signal is sized with
   ``t``'s close as reference and queued for ``t+1``'s open.

Bars before ``trade_start`` are fed to the strategy for warm-up but never traded.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from bot.backtest.costs import CostModel
from bot.backtest.metrics import compute_metrics
from bot.risk.manager import RiskEvent, RiskLimits, RiskManager
from bot.strategies.base import Bar, Signal, Strategy
from bot.strategies.indicators import RollingATR

log = logging.getLogger(__name__)


@dataclass
class Trade:
    symbol: str
    side: int                 # +1 long, -1 short
    qty: int
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    pnl: float                # net of costs
    return_pct: float         # pnl / entry notional
    bars_held: int
    entry_reason: str = ""
    exit_reason: str = ""


@dataclass
class _Position:
    symbol: str
    side: int
    qty: int
    entry_ts: pd.Timestamp
    entry_price: float
    entry_cost: float
    stop: float | None
    entry_reason: str
    bars_held: int = 0


@dataclass
class _Order:
    symbol: str
    side: int          # +1 buy, -1 sell
    qty: int
    reason: str
    signal_ts: pd.Timestamp
    is_exit: bool
    stop: float | None = None
    risk_exit: bool = False   # closed by stop / kill switch rather than by the strategy


@dataclass
class BacktestResult:
    strategy: str
    params: dict[str, Any]
    symbols: list[str]
    equity: pd.Series
    cash: pd.Series
    trades: list[Trade]
    metrics: dict[str, Any]
    benchmark_equity: pd.Series | None = None
    benchmark_metrics: dict[str, Any] | None = None
    risk_events: list[RiskEvent] = field(default_factory=list)
    killed: bool = False
    orders: int = 0
    costs_paid: float = 0.0
    exposure: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def daily_returns(self) -> pd.Series:
        return self.equity.pct_change().dropna()

    def trades_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame(columns=[f.name for f in Trade.__dataclass_fields__.values()])
        return pd.DataFrame([t.__dict__ for t in self.trades])


class Backtester:
    def __init__(self, strategy_factory: Callable[[], Strategy], *, initial_cash: float = 100_000.0,
                 costs: CostModel | None = None, risk: RiskLimits | None = None,
                 trade_start: pd.Timestamp | None = None, trade_end: pd.Timestamp | None = None,
                 benchmark: bool = True, atr_period: int = 14):
        self.strategy_factory = strategy_factory
        self.initial_cash = float(initial_cash)
        self.costs = costs or CostModel()
        self.risk_limits = risk or RiskLimits()
        self.trade_start = pd.Timestamp(trade_start) if trade_start is not None else None
        self.trade_end = pd.Timestamp(trade_end) if trade_end is not None else None
        self.benchmark = benchmark
        self.atr_period = atr_period

    # ------------------------------------------------------------------ run
    def run(self, data: dict[str, pd.DataFrame]) -> BacktestResult:
        data = {s.upper(): df for s, df in data.items()}
        for s, df in data.items():
            if df.empty:
                raise ValueError(f"{s}: no bars")
            if not df.index.is_monotonic_increasing:
                raise ValueError(f"{s}: bars must be sorted")
        symbols = list(data)
        dates = sorted(set().union(*[set(df.index) for df in data.values()]))
        trade_start = self._localize(self.trade_start, dates[0]) if self.trade_start is not None else dates[0]
        trade_end = self._localize(self.trade_end, dates[0]) if self.trade_end is not None else dates[-1]

        strategies: dict[str, Strategy] = {s: self.strategy_factory() for s in symbols}
        atrs = {s: RollingATR(self.atr_period) for s in symbols}
        risk = RiskManager(self.risk_limits)
        # Row lookups by position are much faster than .loc for 10k-bar loops.
        arrays = {s: {c: df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "volume")} for s, df in data.items()}
        pos_of = {s: {ts: i for i, ts in enumerate(df.index)} for s, df in data.items()}

        cash = self.initial_cash
        positions: dict[str, _Position] = {}
        pending: dict[str, _Order] = {}
        last_close: dict[str, float] = {}
        trades: list[Trade] = []
        equity_hist, cash_hist, ts_hist = [], [], []
        risk_events: list[RiskEvent] = []
        n_orders, costs_paid, invested_days = 0, 0.0, 0
        killed_liquidating = False
        strategy_name = strategies[symbols[0]].name
        params = dict(strategies[symbols[0]].params)

        for ts in dates:
            today = {s: pos_of[s][ts] for s in symbols if ts in pos_of[s]}
            trading_day = trade_start <= ts <= trade_end

            # 1. fills at today's open --------------------------------------------------------
            for sym in list(pending):
                if sym not in today:
                    continue
                order = pending.pop(sym)
                o = arrays[sym]["open"][today[sym]]
                fill = self.costs.fill_price(o, order.side)
                comm = self.costs.commission(order.qty)
                n_orders += 1
                costs_paid += abs(order.qty) * abs(fill - o) + comm
                if order.is_exit:
                    p = positions.pop(sym)
                    proceeds = order.qty * fill * -order.side  # sell (+cash) or buy-to-cover (-cash)
                    cash += proceeds - comm
                    gross = p.side * (fill - p.entry_price) * p.qty
                    pnl = gross - p.entry_cost - comm
                    trades.append(Trade(sym, p.side, p.qty, p.entry_ts, p.entry_price, ts, fill, pnl,
                                        pnl / (p.entry_price * p.qty), p.bars_held, p.entry_reason, order.reason))
                    if order.risk_exit:
                        strategies[sym].on_position_closed(sym, order.reason)
                else:
                    qty = order.qty
                    if order.side == 1:  # no leverage: shrink to what cash allows
                        affordable = int(math.floor((cash - comm) / fill)) if fill > 0 else 0
                        qty = min(qty, max(affordable, 0))
                    if qty <= 0:
                        continue
                    cash -= order.side * qty * fill + self.costs.commission(qty)
                    positions[sym] = _Position(sym, order.side, qty, ts, fill,
                                               entry_cost=self.costs.commission(qty),  # slippage is already in `fill`
                                               stop=order.stop, entry_reason=order.reason)

            # 2. mark to market at close ------------------------------------------------------
            for sym, i in today.items():
                last_close[sym] = arrays[sym]["close"][i]
                atrs[sym].update(arrays[sym]["high"][i], arrays[sym]["low"][i], arrays[sym]["close"][i])
            equity = cash + sum(p.side * p.qty * last_close[p.symbol] for p in positions.values())
            for p in positions.values():
                p.bars_held += 1
            if positions:
                invested_days += 1
            equity_hist.append(equity)
            cash_hist.append(cash)
            ts_hist.append(ts)
            if trading_day:
                for ev in risk.update_equity(ts, equity):
                    if ev.kind != "new_day":
                        risk_events.append(ev)
                        log.debug("risk event %s at %s: %s", ev.kind, ts.date(), ev.detail)

            # 3. kill switch -> liquidate everything at the next open, stop trading ---------
            if risk.killed and not killed_liquidating:
                killed_liquidating = True
                for sym, p in positions.items():
                    pending[sym] = _Order(sym, -p.side, p.qty, "kill switch", ts, is_exit=True, risk_exit=True)
            if risk.killed:
                continue

            # 4. protective stops (checked on the completed bar) -----------------------------
            for sym, p in positions.items():
                if sym in today and p.stop is not None and sym not in pending:
                    c = last_close[sym]
                    if (p.side == 1 and c <= p.stop) or (p.side == -1 and c >= p.stop):
                        pending[sym] = _Order(sym, -p.side, p.qty, f"stop {p.stop:.2f} hit (close {c:.2f})", ts, is_exit=True, risk_exit=True)

            # 5. strategy signals -> orders for the next open --------------------------------
            for sym, i in today.items():
                a = arrays[sym]
                bar = Bar(sym, ts, a["open"][i], a["high"][i], a["low"][i], a["close"][i], a["volume"][i])
                sig = strategies[sym].on_bar(bar)
                if sig is None or not trading_day:
                    continue
                self._handle_signal(sig, sym, ts, bar, positions, pending, risk, equity, cash, atrs[sym].atr)

        # Close whatever is still open at the last bar's close (with exit costs) so that the trade list
        # and the equity curve tell the same story. Marked "end of backtest" in the trade log.
        if positions:
            last_ts = ts_hist[-1]
            for sym, p in list(positions.items()):
                fill = self.costs.fill_price(last_close[sym], -p.side)
                comm = self.costs.commission(p.qty)
                cash += p.side * p.qty * fill - comm  # sell a long / buy back a short
                pnl = p.side * (fill - p.entry_price) * p.qty - p.entry_cost - comm
                costs_paid += p.qty * abs(fill - last_close[sym]) + comm
                n_orders += 1
                trades.append(Trade(sym, p.side, p.qty, p.entry_ts, p.entry_price, last_ts, fill, pnl,
                                    pnl / (p.entry_price * p.qty), p.bars_held, p.entry_reason, "end of backtest"))
            positions.clear()
            equity_hist[-1] = cash
            cash_hist[-1] = cash
        equity_s = pd.Series(equity_hist, index=pd.DatetimeIndex(ts_hist), name="equity")
        cash_s = pd.Series(cash_hist, index=equity_s.index, name="cash")
        # Only the trading window counts for metrics (warm-up bars are flat by construction).
        window = (equity_s.index >= trade_start) & (equity_s.index <= trade_end)
        eq_w = equity_s[window]
        n_days = int(window.sum())
        exposure = invested_days / n_days if n_days else 0.0
        metrics = compute_metrics(eq_w, trades, exposure=exposure)
        metrics["costs_paid"] = costs_paid
        metrics["orders"] = n_orders
        metrics["daily_halts"] = sum(1 for e in risk_events if e.kind == "daily_halt")
        metrics["kill_switch"] = bool(risk.killed)
        result = BacktestResult(strategy_name, params, symbols, eq_w, cash_s[window], trades, metrics,
                                risk_events=risk_events, killed=risk.killed, orders=n_orders,
                                costs_paid=costs_paid, exposure=exposure)
        if risk.killed:
            result.notes.append(f"KILL SWITCH TRIPPED: {risk.state.kill_reason}. All positions liquidated; no trading afterwards.")
        if self.benchmark:
            result.benchmark_equity = self._buy_and_hold(data, eq_w.index)
            result.benchmark_metrics = compute_metrics(result.benchmark_equity)
        return result

    # -------------------------------------------------------------- helpers
    def _handle_signal(self, sig: Signal, sym: str, ts, bar: Bar, positions, pending, risk: RiskManager,
                       equity: float, cash: float, atr_value: float | None) -> None:
        pos = positions.get(sym)
        cur = pos.side if pos else 0
        if sig.target == cur:
            return
        if pos is not None:  # exit (also the first leg of a reversal; re-entry happens on a later signal)
            if sym in pending and pending[sym].is_exit:
                return  # a stop / kill exit is already queued for this bar; keep its reason
            pending[sym] = _Order(sym, -pos.side, pos.qty, sig.reason or "signal exit", ts, is_exit=True)
            return
        if sym in pending:  # an exit is already queued for this bar; don't stack orders
            return
        ok, why = risk.can_open(len(positions) + sum(1 for o in pending.values() if not o.is_exit))
        if not ok:
            log.debug("%s %s: entry blocked (%s)", ts.date(), sym, why)
            return
        stop_dist = risk.stop_distance(atr_value, bar.close)
        if sig.stop_price is not None:
            stop_dist = abs(bar.close - sig.stop_price)
        qty = risk.position_qty(equity, bar.close, stop_dist, cash_available=cash if sig.target == 1 else None)
        if qty <= 0:
            return
        stop = sig.stop_price if sig.stop_price is not None else bar.close - sig.target * stop_dist
        pending[sym] = _Order(sym, sig.target, qty, sig.reason, ts, is_exit=False, stop=stop)

    @staticmethod
    def _localize(ts: pd.Timestamp, like: pd.Timestamp) -> pd.Timestamp:
        if ts.tzinfo is None and like.tzinfo is not None:
            return ts.tz_localize(like.tzinfo)
        if ts.tzinfo is not None and like.tzinfo is None:
            return ts.tz_localize(None)
        return ts

    def _buy_and_hold(self, data: dict[str, pd.DataFrame], index: pd.DatetimeIndex) -> pd.Series:
        """Equal-weight buy-and-hold of the same symbols, bought at the first open with costs."""
        parts = []
        for sym, df in data.items():
            d = df.reindex(index).ffill()
            first = d["open"].dropna()
            if first.empty:
                continue
            entry = self.costs.fill_price(float(first.iloc[0]), +1)
            parts.append(d["close"] / entry)
        if not parts:
            return pd.Series(self.initial_cash, index=index)
        rel = pd.concat(parts, axis=1).mean(axis=1)
        return (rel * self.initial_cash).rename("benchmark")
