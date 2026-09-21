"""Teaching baseline #1: moving-average crossover (long/flat).

Long when the fast SMA is above the slow SMA, flat otherwise. This is the
canonical trend-following toy; it is *not* claimed to have edge.
"""
from __future__ import annotations

from typing import Any, ClassVar

from bot.strategies.base import Bar, Signal, Strategy
from bot.strategies.indicators import RollingMean


class MACrossover(Strategy):
    name: ClassVar[str] = "ma_crossover"
    default_params: ClassVar[dict[str, Any]] = {"fast": 50, "slow": 200}

    @property
    def warmup(self) -> int:
        return int(self.params["slow"]) + 1

    def reset(self) -> None:
        fast, slow = int(self.params["fast"]), int(self.params["slow"])
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self.fast = RollingMean(fast)
        self.slow = RollingMean(slow)
        self.state: int | None = None  # last emitted target

    def on_bar(self, bar: Bar) -> Signal | None:
        f = self.fast.update(bar.close)
        s = self.slow.update(bar.close)
        if f is None or s is None:
            return None
        target = 1 if f > s else 0
        if target == self.state:
            return None
        self.state = target
        return Signal(bar.symbol, target, reason=f"fast={f:.2f} {'>' if target else '<='} slow={s:.2f}")

    def on_position_closed(self, symbol: str, reason: str) -> None:
        self.state = None  # re-evaluate on the next bar; re-enters if the trend condition still holds

    @classmethod
    def param_grid(cls) -> list[dict[str, Any]]:
        return [{"fast": f, "slow": s} for f in (10, 20, 50) for s in (50, 100, 200) if f < s]
