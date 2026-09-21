"""Portfolio-level risk limits shared by the backtester and the live/paper loop.

- daily loss limit: after equity falls ``daily_loss_limit_pct`` below the day's
  starting equity, no new positions are opened until the next trading day.
- max drawdown kill switch: once equity is ``max_drawdown_pct`` below its peak,
  everything is liquidated and trading stops until a human resets the state.
- max concurrent positions.
- position sizing: fixed fractional risk with an ATR-based protective stop.

State is a plain dataclass so it can be persisted and restored across restarts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from bot.risk.sizing import fixed_fractional_qty


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.50
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.20
    max_positions: int = 5
    atr_stop_mult: float = 2.0

    @classmethod
    def from_settings(cls, s) -> "RiskLimits":
        return cls(s.risk_per_trade_pct, s.max_position_pct, s.daily_loss_limit_pct,
                   s.max_drawdown_pct, s.max_positions, s.atr_stop_mult)


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str | None = None            # ISO date of the current trading day
    day_start_equity: float = 0.0
    last_equity: float = 0.0
    halted_today: bool = False
    killed: bool = False
    kill_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RiskState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class RiskEvent:
    kind: str          # "daily_halt" | "kill_switch" | "new_day"
    ts: pd.Timestamp
    equity: float
    detail: str = ""


class RiskManager:
    def __init__(self, limits: RiskLimits, state: RiskState | None = None):
        self.limits = limits
        self.state = state or RiskState()

    # ---------------------------------------------------------------- equity
    def update_equity(self, ts: pd.Timestamp, equity: float) -> list[RiskEvent]:
        """Feed a new equity mark. Returns any limit events triggered by this mark."""
        s, L = self.state, self.limits
        events: list[RiskEvent] = []
        d = pd.Timestamp(ts).date().isoformat()
        if s.day != d:
            # New trading day: the reference is the last mark of the prior day (or this mark if none).
            s.day = d
            s.day_start_equity = s.last_equity if s.last_equity > 0 else equity
            s.halted_today = False
            events.append(RiskEvent("new_day", ts, equity))
        s.last_equity = equity
        if equity > s.peak_equity:
            s.peak_equity = equity
        if not s.killed and s.peak_equity > 0:
            dd = 1 - equity / s.peak_equity
            if dd >= L.max_drawdown_pct:
                s.killed = True
                s.kill_reason = f"drawdown {dd:.1%} >= limit {L.max_drawdown_pct:.1%} (peak {s.peak_equity:.2f})"
                events.append(RiskEvent("kill_switch", ts, equity, s.kill_reason))
        if not s.halted_today and s.day_start_equity > 0:
            day_ret = equity / s.day_start_equity - 1
            if day_ret <= -L.daily_loss_limit_pct:
                s.halted_today = True
                events.append(RiskEvent("daily_halt", ts, equity,
                                        f"day P&L {day_ret:.2%} <= -{L.daily_loss_limit_pct:.1%}"))
        return events

    # ---------------------------------------------------------------- gates
    @property
    def killed(self) -> bool:
        return self.state.killed

    @property
    def halted_today(self) -> bool:
        return self.state.halted_today

    def can_open(self, n_open_positions: int) -> tuple[bool, str]:
        if self.state.killed:
            return False, "kill switch active"
        if self.state.halted_today:
            return False, "daily loss limit hit"
        if n_open_positions >= self.limits.max_positions:
            return False, f"max positions ({self.limits.max_positions}) reached"
        return True, ""

    def reset_kill_switch(self) -> None:
        """Manual, deliberate human action only."""
        self.state.killed = False
        self.state.kill_reason = ""
        self.state.peak_equity = self.state.last_equity

    # --------------------------------------------------------------- sizing
    def stop_distance(self, atr_value: float | None, price: float) -> float:
        if atr_value and atr_value > 0:
            return self.limits.atr_stop_mult * atr_value
        return 0.02 * price  # conservative default when ATR isn't ready

    def position_qty(self, equity: float, price: float, stop_distance: float, cash_available: float | None = None) -> int:
        return fixed_fractional_qty(equity, price, stop_distance, risk_pct=self.limits.risk_per_trade_pct,
                                    max_position_pct=self.limits.max_position_pct, cash_available=cash_available)
