"""Teaching baseline #2: z-score mean reversion.

z = (close - rolling_mean) / rolling_std. Enter long when z < -entry_z, exit
when z > -exit_z. Optionally mirror on the short side. Also not claimed alpha.
"""
from __future__ import annotations

from typing import Any, ClassVar

from bot.strategies.base import Bar, Signal, Strategy
from bot.strategies.indicators import RollingStats


class MeanReversion(Strategy):
    name: ClassVar[str] = "mean_reversion"
    default_params: ClassVar[dict[str, Any]] = {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5, "allow_short": False}

    @property
    def warmup(self) -> int:
        return int(self.params["lookback"]) + 1

    def reset(self) -> None:
        self.stats = RollingStats(int(self.params["lookback"]))
        self.position = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        res = self.stats.update(bar.close)
        if res is None:
            return None
        mean, std = res
        if std <= 0:
            return None
        z = (bar.close - mean) / std
        entry, exit_ = float(self.params["entry_z"]), float(self.params["exit_z"])
        target = self.position
        if self.position == 0:
            if z < -entry:
                target = 1
            elif self.params["allow_short"] and z > entry:
                target = -1
        elif self.position == 1 and z > -exit_:
            target = 0
        elif self.position == -1 and z < exit_:
            target = 0
        if target == self.position:
            return None
        self.position = target
        return Signal(bar.symbol, target, reason=f"z={z:.2f}")

    def on_position_closed(self, symbol: str, reason: str) -> None:
        self.position = 0

    @classmethod
    def param_grid(cls) -> list[dict[str, Any]]:
        return [{"lookback": lb, "entry_z": e, "exit_z": x}
                for lb in (10, 20, 40) for e in (1.5, 2.0, 2.5) for x in (0.0, 0.5)]
