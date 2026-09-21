"""Transaction cost model: slippage + half-spread per side, optional per-share commission."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    slippage_bps: float = 2.0      # adverse price move vs. the reference price, per side
    spread_bps: float = 2.0        # full quoted spread; we pay half of it per side
    commission_per_share: float = 0.0

    @classmethod
    def from_settings(cls, s) -> "CostModel":
        return cls(s.slippage_bps, s.spread_bps, s.commission_per_share)

    @property
    def per_side_fraction(self) -> float:
        return (self.slippage_bps + self.spread_bps / 2) / 10_000

    def fill_price(self, reference: float, side: int) -> float:
        """side=+1 buy (pay more), side=-1 sell (receive less)."""
        return reference * (1 + side * self.per_side_fraction)

    def commission(self, qty: int) -> float:
        return abs(qty) * self.commission_per_share

    def round_trip_bps(self) -> float:
        return 2 * (self.slippage_bps + self.spread_bps / 2)
