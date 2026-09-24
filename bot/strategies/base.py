"""Strategy interface.

Strategies are *event-driven*: the engine feeds them one completed bar at a
time via ``on_bar`` and they may only use what they have already seen. This
makes lookahead structurally impossible - a strategy has no handle to future
data. ``generate_signals`` is a convenience that replays a DataFrame through
``on_bar`` for research/plots.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import pandas as pd


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_row(cls, symbol: str, ts: pd.Timestamp, row) -> "Bar":
        return cls(symbol, ts, float(row["open"]), float(row["high"]), float(row["low"]),
                   float(row["close"]), float(row.get("volume", 0.0) or 0.0))


@dataclass(frozen=True)
class Signal:
    """Desired exposure after this bar: +1 long, -1 short, 0 flat.

    ``stop_price`` is optional; when absent the engine derives a protective stop
    from ATR (see risk settings). ``reason`` is free text for logs/reports.
    """
    symbol: str
    target: int
    reason: str = ""
    stop_price: float | None = None

    def __post_init__(self):
        if self.target not in (-1, 0, 1):
            raise ValueError("target must be -1, 0 or 1")


class Strategy(ABC):
    """Base class. Subclasses declare ``name``, ``default_params`` and implement ``on_bar``."""

    name: ClassVar[str] = "base"
    default_params: ClassVar[dict[str, Any]] = {}

    def __init__(self, **params: Any):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params: dict[str, Any] = {**self.default_params, **params}
        self.reset()

    # ---- interface --------------------------------------------------------
    @property
    @abstractmethod
    def warmup(self) -> int:
        """Number of bars needed before the first valid signal."""

    @abstractmethod
    def reset(self) -> None:
        """Clear all internal state (called by __init__ and at the start of every run)."""

    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None:
        """Consume one completed bar; return a Signal or None (= no opinion / hold)."""

    def on_position_closed(self, symbol: str, reason: str) -> None:
        """Called when the *risk layer* (stop / kill switch) closed a position the strategy did not
        ask to close. Strategies should reset their view so they can re-signal if still valid."""

    @classmethod
    def param_grid(cls) -> list[dict[str, Any]]:
        """Parameter combinations for walk-forward optimisation. Default: only the defaults."""
        return [dict(cls.default_params)]

    # ---- convenience ------------------------------------------------------
    def generate_signals(self, df: pd.DataFrame, symbol: str = "X") -> pd.Series:
        """Replay ``df`` (OHLCV indexed by timestamp) and return the target exposure per bar."""
        self.reset()
        out = []
        last = 0
        for ts, row in df.iterrows():
            sig = self.on_bar(Bar.from_row(symbol, ts, row))
            if sig is not None:
                last = sig.target
            out.append(last)
        return pd.Series(out, index=df.index, name="target")

    def describe(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in self.params.items())})"
