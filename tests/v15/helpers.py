"""Shared helpers for V1.5 tests."""
from datetime import date, datetime

import numpy as np
import pandas as pd

from bot.core.events import BarEvent
from bot.core.intents import TradeIntent
from bot.data.calendar import NY


def bar(symbol="SPY", ts=None, o=100.0, h=None, l=None, c=None, v=10_000.0, tf="1m", d=None, end=False) -> BarEvent:
    ts = ts or datetime(2026, 9, 25, 10, 0, tzinfo=NY)
    c = o if c is None else c
    return BarEvent(symbol, ts, o, h if h is not None else max(o, c) + 0.05, l if l is not None else min(o, c) - 0.05, c, v, tf, d or ts.date(), end)


def intent(symbol="SPY", module="T", direction=1, price=100.0, vol=0.01, risk=0.01, style="market", exit_style="market",
           overnight=False, stop=None, weight=None, ts=None) -> TradeIntent:
    return TradeIntent(symbol, module, ts or datetime(2026, 9, 25, 15, 30, tzinfo=NY), direction, 1800, price, vol, risk, 1.0, None, None,
                       1800, style, exit_style, overnight, stop, "t", weight)


def minute_session(d: date, *, start=(9, 30), minutes=390, base=500.0, seed=0, drift=0.0, symbol="SPY") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(datetime(d.year, d.month, d.day, *start, tzinfo=NY), periods=minutes, freq="1min")
    close = base + np.cumsum(rng.normal(drift, 0.05, minutes))
    return pd.DataFrame({"open": close - 0.01, "high": close + 0.04, "low": close - 0.04, "close": close,
                         "volume": rng.integers(5_000, 20_000, minutes).astype(float), "vwap": close}, index=idx)


def minute_history(sessions: list[date], **kw) -> pd.DataFrame:
    frames, base = [], kw.pop("base", 500.0)
    for i, d in enumerate(sessions):
        df = minute_session(d, base=base, seed=i, **kw)
        base = float(df["close"].iloc[-1])
        frames.append(df)
    return pd.concat(frames)
