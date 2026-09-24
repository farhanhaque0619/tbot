"""Research-only candidate strategies (Phase 8). NOT registered for execution.

Each candidate documents: hypothesis, rationale, exact rule, expected failure mode, cost sensitivity. Results and
parameter surfaces live in RESEARCH.md; the hypotheses in research/HYPOTHESES.md.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, ClassVar

from bot.strategies.base import Bar, Signal, Strategy
from bot.strategies.indicators import RollingMean


class DonchianBreakout(Strategy):
    """H1 breakout confirmation: enter long on a close above the prior N-day high; exit on a close below the prior
    M-day low. Rationale: trend continuation after a range expansion; avoids the MA lag. Failure mode: false
    breakouts in ranges (whipsaw), gap entries at bad prices."""
    name: ClassVar[str] = "donchian_breakout"
    default_params: ClassVar[dict[str, Any]] = {"entry_n": 55, "exit_n": 20}

    @property
    def warmup(self) -> int:
        return int(self.params["entry_n"]) + 1

    def reset(self) -> None:
        self.highs: deque[float] = deque(maxlen=int(self.params["entry_n"]))
        self.lows: deque[float] = deque(maxlen=int(self.params["exit_n"]))
        self.position = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        prior_high = max(self.highs) if len(self.highs) == self.highs.maxlen else None
        prior_low = min(self.lows) if len(self.lows) == self.lows.maxlen else None
        self.highs.append(bar.high)
        self.lows.append(bar.low)
        if prior_high is None or prior_low is None:
            return None
        target = self.position
        if self.position == 0 and bar.close > prior_high:
            target = 1
        elif self.position == 1 and bar.close < prior_low:
            target = 0
        if target == self.position:
            return None
        self.position = target
        return Signal(bar.symbol, target, reason=f"close {bar.close:.2f} vs {'high' if target else 'low'} {(prior_high if target else prior_low):.2f}")

    def on_position_closed(self, symbol: str, reason: str) -> None:
        self.position = 0

    @classmethod
    def param_grid(cls) -> list[dict[str, Any]]:
        return [{"entry_n": e, "exit_n": x} for e in (20, 55, 100) for x in (10, 20, 50) if x < e]


class MACrossoverBuffered(Strategy):
    """H2 whipsaw reduction: MA crossover with a hysteresis band. Long when fast > slow*(1+band), flat when fast <
    slow*(1-band). Rationale: most MA-crossover losses come from repeated crosses around a flat slow MA; a band
    demands a minimum separation. Failure mode: later entries/exits give back more in fast reversals."""
    name: ClassVar[str] = "ma_crossover_buffered"
    default_params: ClassVar[dict[str, Any]] = {"fast": 50, "slow": 200, "band": 0.01}

    @property
    def warmup(self) -> int:
        return int(self.params["slow"]) + 1

    def reset(self) -> None:
        self.fast = RollingMean(int(self.params["fast"]))
        self.slow = RollingMean(int(self.params["slow"]))
        self.state = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        f, s = self.fast.update(bar.close), self.slow.update(bar.close)
        if f is None or s is None:
            return None
        band = float(self.params["band"])
        target = self.state
        if self.state == 0 and f > s * (1 + band):
            target = 1
        elif self.state == 1 and f < s * (1 - band):
            target = 0
        if target == self.state:
            return None
        self.state = target
        return Signal(bar.symbol, target, reason=f"fast={f:.2f} slow={s:.2f} band={band}")

    def on_position_closed(self, symbol: str, reason: str) -> None:
        self.state = 0

    @classmethod
    def param_grid(cls) -> list[dict[str, Any]]:
        return [{"fast": f, "slow": s, "band": b} for f, s in ((20, 100), (50, 200)) for b in (0.0, 0.005, 0.01, 0.02)]


class TrendVolFilter(Strategy):
    """H3 volatility-regime filter: MA crossover long only while trailing realised volatility is below a cap; exit
    when volatility rises above it. Rationale: trend strategies' worst drawdowns cluster in high-vol regimes;
    sitting out reduces tail risk. Failure mode: misses the sharp V-shaped recoveries that start in high vol
    (2009, 2020)."""
    name: ClassVar[str] = "trend_vol_filter"
    default_params: ClassVar[dict[str, Any]] = {"fast": 50, "slow": 200, "vol_window": 21, "vol_cap": 0.25}

    @property
    def warmup(self) -> int:
        return int(self.params["slow"]) + 1

    def reset(self) -> None:
        self.fast = RollingMean(int(self.params["fast"]))
        self.slow = RollingMean(int(self.params["slow"]))
        self.rets: deque[float] = deque(maxlen=int(self.params["vol_window"]))
        self.prev: float | None = None
        self.state = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        if self.prev:
            self.rets.append(math.log(bar.close / self.prev))
        self.prev = bar.close
        f, s = self.fast.update(bar.close), self.slow.update(bar.close)
        if f is None or s is None or len(self.rets) < self.rets.maxlen:
            return None
        m = sum(self.rets) / len(self.rets)
        vol = math.sqrt(sum((r - m) ** 2 for r in self.rets) / (len(self.rets) - 1)) * math.sqrt(252)
        target = 1 if (f > s and vol < float(self.params["vol_cap"])) else 0
        if target == self.state:
            return None
        self.state = target
        return Signal(bar.symbol, target, reason=f"fast={f:.2f} slow={s:.2f} vol={vol:.2f}")

    def on_position_closed(self, symbol: str, reason: str) -> None:
        self.state = 0

    @classmethod
    def param_grid(cls) -> list[dict[str, Any]]:
        return [{"fast": 50, "slow": 200, "vol_window": 21, "vol_cap": c} for c in (0.15, 0.20, 0.25, 0.35, 10.0)]


RESEARCH_STRATEGIES: dict[str, type[Strategy]] = {c.name: c for c in (DonchianBreakout, MACrossoverBuffered, TrendVolFilter)}
