"""Fill models for the V1.5 backtester (Phase 2). All parameters explicit, defaults conservative.

A fill may only use bars strictly AFTER the decision timestamp (enforced by the engine, which hands the model the
next bar). Prices: the reference is the next 1-minute bar's open (marketable limit / market), the session's official
open (OPG) or close (CLS), or the stop level (stop orders, elected on the first bar whose low/high crosses).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FillParams:
    slippage_bps: float = 2.0
    market_extra_bps: float = 1.0
    open_auction_slippage_bps: float = 3.0
    close_auction_slippage_bps: float = 1.0
    stop_slippage_bps: float = 5.0
    participation_cap: float = 0.01              # fraction of the bar's dollar volume a single order may take
    default_spread_bps_etf: float = 1.0
    default_spread_bps_stock: float = 3.0
    # cost-stress hooks (all default to "off")
    spread_mult: float = 1.0
    slippage_mult: float = 1.0
    execution_delay_bars: int = 0
    drop_signal_fraction: float = 0.0
    adverse_fill_prob: float = 0.0
    adverse_fill_bps: float = 0.0
    seed: int = 0
    # legacy reproduction: V1 daily engine costs (slippage + half spread, applied to the next open)
    legacy_spread_bps: float = 2.0
    commission_per_share: float = 0.0

    def spread_bps(self, symbol_kind: str, quoted: float | None) -> float:
        base = quoted if (quoted is not None and quoted == quoted and quoted >= 0) else (
            self.default_spread_bps_etf if symbol_kind == "etf" else self.default_spread_bps_stock)
        return base * self.spread_mult


@dataclass
class Fill:
    qty: float                 # filled this step (positive)
    price: float
    remaining: float
    reason: str
    costs_bps: float


class FillEngine:
    """Stateless price logic plus a seeded RNG for the adverse-fill stress hook."""

    def __init__(self, params: FillParams | None = None):
        self.p = params or FillParams()
        self.rng = random.Random(self.p.seed)

    # ---------------------------------------------------------------- helpers
    def _adverse(self) -> float:
        if self.p.adverse_fill_prob > 0 and self.rng.random() < self.p.adverse_fill_prob:
            return self.p.adverse_fill_bps
        return 0.0

    def _cap_qty(self, want: float, bar_dollar_volume: float, price: float) -> float:
        if self.p.participation_cap <= 0 or bar_dollar_volume <= 0:
            return want
        cap = self.p.participation_cap * bar_dollar_volume / price
        return min(want, cap)

    # ---------------------------------------------------------------- models
    def marketable_limit(self, side: int, qty: float, next_bar: Any, *, symbol_kind: str, quoted_spread_bps: float | None,
                         limit_price: float | None = None, market: bool = False) -> Fill | None:
        """Fill at next_bar.open ± (half spread + slippage [+ market extra]). A limit is respected: no fill if the
        reference lands beyond the limit."""
        half_spread = self.p.spread_bps(symbol_kind, quoted_spread_bps) / 2
        bps = half_spread + self.p.slippage_bps * self.p.slippage_mult + (self.p.market_extra_bps if market else 0.0) + self._adverse()
        price = next_bar.open * (1 + side * bps / 1e4)
        if limit_price is not None and ((side > 0 and price > limit_price + 1e-9) or (side < 0 and price < limit_price - 1e-9)):
            return None
        filled = self._cap_qty(qty, next_bar.volume * next_bar.open, price)
        return Fill(filled, price, qty - filled, "market" if market else "marketable_limit", bps)

    def market(self, side: int, qty: float, next_bar: Any, *, symbol_kind: str, quoted_spread_bps: float | None) -> Fill | None:
        return self.marketable_limit(side, qty, next_bar, symbol_kind=symbol_kind, quoted_spread_bps=quoted_spread_bps, market=True)

    def auction_open(self, side: int, qty: float, official_open: float) -> Fill:
        bps = self.p.open_auction_slippage_bps * self.p.slippage_mult + self._adverse()
        return Fill(qty, official_open * (1 + side * bps / 1e4), 0.0, "opg", bps)

    def auction_close(self, side: int, qty: float, official_close: float) -> Fill:
        bps = self.p.close_auction_slippage_bps * self.p.slippage_mult + self._adverse()
        return Fill(qty, official_close * (1 + side * bps / 1e4), 0.0, "cls", bps)

    @staticmethod
    def stop_elected(side: int, stop_price: float, bar: Any) -> bool:
        """A sell stop elects when the bar's low <= stop; a buy stop when the bar's high >= stop."""
        return bar.low <= stop_price if side < 0 else bar.high >= stop_price

    def stop_fill(self, side: int, qty: float, stop_price: float, fill_bar: Any, *, gapped: bool) -> Fill:
        """Fill at the bar AFTER election, at its open, minus/plus stop slippage. If the open has already gapped
        through the stop, the open is the price (gap risk realised)."""
        bps = self.p.stop_slippage_bps * self.p.slippage_mult + self._adverse()
        ref = fill_bar.open
        if not gapped:
            ref = min(fill_bar.open, stop_price) if side < 0 else max(fill_bar.open, stop_price)
        price = ref * (1 + side * bps / 1e4)
        return Fill(qty, price, 0.0, "stop", bps)

    def legacy_daily(self, side: int, qty: float, next_open: float) -> Fill:
        """V1 engine: open * (1 ± (slippage + spread/2)/1e4), whole fill, commission separate."""
        bps = self.p.slippage_bps + self.p.legacy_spread_bps / 2
        return Fill(qty, next_open * (1 + side * bps / 1e4), 0.0, "daily_legacy", bps)
