"""Small incremental indicators (O(1) per bar) so strategies stay event-driven."""
from __future__ import annotations

import math
from collections import deque

import pandas as pd


class RollingMean:
    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.sum = 0.0

    def update(self, x: float) -> float | None:
        if len(self.buf) == self.n:
            self.sum -= self.buf[0]
        self.buf.append(x)
        self.sum += x
        return self.sum / self.n if len(self.buf) == self.n else None

    @property
    def ready(self) -> bool:
        return len(self.buf) == self.n


class RollingStats:
    """Rolling mean and sample std over the last n values (numerically stable enough for prices)."""

    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)

    def update(self, x: float) -> tuple[float, float] | None:
        self.buf.append(x)
        if len(self.buf) < self.n:
            return None
        m = sum(self.buf) / self.n
        var = sum((v - m) ** 2 for v in self.buf) / (self.n - 1)
        return m, math.sqrt(var)


class RollingATR:
    """Wilder's Average True Range, incremental."""

    def __init__(self, n: int = 14):
        self.n = n
        self.prev_close: float | None = None
        self.atr: float | None = None
        self._seed: list[float] = []

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        if self.atr is None:
            self._seed.append(tr)
            if len(self._seed) == self.n:
                self.atr = sum(self._seed) / self.n
            return self.atr
        self.atr = (self.atr * (self.n - 1) + tr) / self.n
        return self.atr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Vectorised Wilder ATR (matches RollingATR)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
