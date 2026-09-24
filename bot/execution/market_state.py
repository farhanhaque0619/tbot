"""Typed MarketState, assembled entirely in ordinary code (Phase 12).

Every field is computed deterministically from bars, quotes, the account snapshot and the bot's own state.
No model generates these values; models (if any) may only consume them.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from bot.execution.broker import AccountInfo, BrokerPosition, OrderInfo, QuoteInfo
from bot.strategies.indicators import atr as atr_series

TRADING_DAYS = 252


@dataclass(frozen=True)
class MarketState:
    timestamp: str
    symbol: str
    market_open: bool
    last_price: float
    bid: float
    ask: float
    mid: float
    spread_bps: float
    bar_return_1: float
    bar_return_n: float
    atr: float
    realized_vol: float
    fast_ma: float
    slow_ma: float
    distance_from_ma: float
    volume: float
    volume_zscore: float
    position_qty: float
    position_notional: float
    cash: float
    equity: float
    buying_power: float
    gross_exposure: float
    net_exposure: float
    daily_pnl: float
    drawdown: float
    open_orders: int
    stale_data_seconds: float
    last_bar_date: str
    n_bars: int

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v) for k, v in d.items()}


def _f(x, default: float = float("nan")) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def build_market_state(*, symbol: str, bars: pd.DataFrame, now: datetime, market_open: bool,
                       quote: QuoteInfo | None, account: AccountInfo | None,
                       positions: dict[str, BrokerPosition], open_orders: list[OrderInfo],
                       peak_equity: float = 0.0, day_start_equity: float = 0.0,
                       fast: int = 50, slow: int = 200, n: int = 20) -> MarketState:
    close = bars["close"].astype(float) if not bars.empty else pd.Series(dtype=float)
    last_price = _f(close.iloc[-1]) if len(close) else float("nan")
    r1 = _f(close.iloc[-1] / close.iloc[-2] - 1) if len(close) >= 2 else float("nan")
    rn = _f(close.iloc[-1] / close.iloc[-n - 1] - 1) if len(close) > n else float("nan")
    logret = np.log(close).diff().dropna()
    rvol = _f(logret.tail(n).std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(logret) >= n else float("nan")
    atr_v = _f(atr_series(bars, 14).iloc[-1]) if len(bars) >= 15 else float("nan")
    fma = _f(close.tail(fast).mean()) if len(close) >= fast else float("nan")
    sma = _f(close.tail(slow).mean()) if len(close) >= slow else float("nan")
    dist = _f(last_price / sma - 1) if math.isfinite(sma) and sma > 0 else float("nan")
    vol = _f(bars["volume"].iloc[-1]) if "volume" in bars and len(bars) else float("nan")
    if "volume" in bars and len(bars) > n:
        v = bars["volume"].astype(float).tail(n + 1)
        sd = v.iloc[:-1].std(ddof=1)
        vz = _f((v.iloc[-1] - v.iloc[:-1].mean()) / sd) if sd and sd > 0 else float("nan")
    else:
        vz = float("nan")
    if quote is not None:
        bid, ask, mid, spread = quote.bid, quote.ask, quote.mid, quote.spread_bps
        stale = max(0.0, (now - quote.timestamp).total_seconds())
    else:
        bid = ask = mid = spread = float("nan")
        stale = max(0.0, (now - bars.index[-1].to_pydatetime()).total_seconds()) if len(bars) else float("inf")
    pos = positions.get(symbol)
    pq = pos.qty if pos else 0.0
    ref = mid if math.isfinite(mid) and mid > 0 else last_price
    pn = pq * ref if math.isfinite(ref) else 0.0
    gross = sum(abs(p.market_value) for p in positions.values())
    net = sum(p.market_value for p in positions.values())
    eq = account.equity if account else float("nan")
    cash = account.cash if account else float("nan")
    bp = account.buying_power if account else float("nan")
    dd = _f(1 - eq / peak_equity) if peak_equity and peak_equity > 0 and math.isfinite(eq) else 0.0
    dpnl = _f(eq - day_start_equity) if day_start_equity and math.isfinite(eq) else 0.0
    return MarketState(
        timestamp=now.isoformat(), symbol=symbol, market_open=bool(market_open), last_price=last_price,
        bid=bid, ask=ask, mid=mid, spread_bps=spread, bar_return_1=r1, bar_return_n=rn, atr=atr_v,
        realized_vol=rvol, fast_ma=fma, slow_ma=sma, distance_from_ma=dist, volume=vol, volume_zscore=vz,
        position_qty=pq, position_notional=pn, cash=cash, equity=eq, buying_power=bp,
        gross_exposure=gross, net_exposure=net, daily_pnl=dpnl, drawdown=max(dd, 0.0),
        open_orders=sum(1 for o in open_orders if o.symbol == symbol),
        stale_data_seconds=stale, last_bar_date=bars.index[-1].date().isoformat() if len(bars) else "",
        n_bars=int(len(bars)),
    )
