"""TradeIntent (strategy output) and TargetPosition (allocator output). Strategies express opinions, never sizes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

ENTRY_STYLES = ("marketable_limit", "opg", "cls", "market", "market_1555", "limit_at_prev_close_cancel_0945")
EXIT_STYLES = ("cls", "market_1558", "market_1555", "marketable_limit", "opg", "market")


@dataclass(frozen=True)
class TradeIntent:
    symbol: str
    module_id: str
    ts: datetime
    direction: int                       # +1 long, -1 short, 0 flat (close this module's slice)
    horizon_seconds: int
    reference_price: float
    volatility: float                    # sigma over the horizon, fraction of price
    risk_budget_pct: float               # module default; the allocator may only scale DOWN
    signal_strength: float               # [0, 1]
    expected_edge_bps: float | None
    invalidation_price: float | None
    max_holding_seconds: int
    entry_style: str
    exit_style: str
    overnight_ok: bool
    protective_stop_price: float | None
    tag: str = ""
    target_weight: float | None = None   # M1 only: the allocator treats the intent as a target weight

    def __post_init__(self):
        if self.direction not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0 or 1")
        if self.entry_style not in ENTRY_STYLES:
            raise ValueError(f"unknown entry_style {self.entry_style!r}")
        if self.exit_style not in EXIT_STYLES:
            raise ValueError(f"unknown exit_style {self.exit_style!r}")
        if not (0.0 <= self.signal_strength <= 1.0):
            raise ValueError("signal_strength must be in [0, 1]")
        if self.risk_budget_pct < 0:
            raise ValueError("risk_budget_pct must be >= 0")


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    module_id: str
    target_qty: float                    # signed
    notional: float                      # abs target notional at reference price
    reference_price: float
    entry_style: str
    exit_style: str
    protective_stop_price: float | None
    overnight_ok: bool
    intent: TradeIntent | None = None
    scaling_log: tuple[str, ...] = field(default_factory=tuple)
    kind: str = "entry"                  # entry | exit | rebalance

    @property
    def is_flat(self) -> bool:
        return abs(self.target_qty) < 1e-12
