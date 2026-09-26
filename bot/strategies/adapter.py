"""LegacyStrategyAdapter (Phase 2, spec §5.1): wraps a V1 ``Strategy`` (on_bar -> Signal) as a V1.5 module that emits
TradeIntents on daily close events. Sizing/stop semantics reproduce the V1 engine: risk = policy.legacy_risk_pct
against a 2×ATR(14) stop (or 2% of price before ATR is ready), volatility = stop_distance / price so that the
allocator's ``risk / sigma`` gives V1's ``risk_dollars / stop_distance`` quantity; entries and exits at the next open
(style ``market`` so fractional rounding matches V1; the legacy fill step treats every market order as next-open).
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.core.events import BarEvent
from bot.core.intents import TradeIntent
from bot.core.policy import RiskPolicy
from bot.features.engine import FeatureSnapshot
from bot.strategies.base import Bar, Strategy


@dataclass
class PositionView:
    qty: float = 0.0

    @property
    def side(self) -> int:
        return 0 if abs(self.qty) < 1e-12 else (1 if self.qty > 0 else -1)


class LegacyStrategyAdapter:
    def __init__(self, strategy: Strategy, policy: RiskPolicy, *, symbol: str):
        self.strategy = strategy
        self.policy = policy
        self.symbol = symbol.upper()
        self.module_id = strategy.name
        self.strategy.reset()

    def stop_distance(self, atr: float | None, price: float) -> float:
        if atr and atr > 0:
            return self.policy.legacy_atr_stop_mult * atr
        return 0.02 * price

    def on_event(self, ev: BarEvent, snapshot: FeatureSnapshot, position: PositionView) -> list[TradeIntent]:
        if ev.timeframe != "1d" or ev.symbol.upper() != self.symbol:
            return []
        sig = self.strategy.on_bar(Bar(ev.symbol, ev.ts, ev.open, ev.high, ev.low, ev.close, ev.volume))
        if sig is None:
            return []
        cur = position.side
        if sig.target == cur:
            return []
        common = dict(symbol=self.symbol, module_id=self.module_id, ts=ev.ts, horizon_seconds=86400, reference_price=ev.close,
                      signal_strength=1.0, expected_edge_bps=None, invalidation_price=None, max_holding_seconds=365 * 86400,
                      entry_style="market", exit_style="market", overnight_ok=True, tag=sig.reason)   # legacy fill = next open
        if cur != 0:  # V1: a differing signal while in a position is an exit, never a reversal in one step
            return [TradeIntent(direction=0, volatility=0.0, risk_budget_pct=0.0, protective_stop_price=None, **common)]
        if sig.stop_price is not None:
            stop_dist = abs(ev.close - sig.stop_price)
        else:
            stop_dist = self.stop_distance(snapshot.atr14_d, ev.close)
        vol = stop_dist / ev.close if ev.close > 0 else 0.0
        stop = (sig.stop_price if sig.stop_price is not None else ev.close - sig.target * stop_dist) if self.policy.legacy_atr_stop else None
        return [TradeIntent(direction=sig.target, volatility=vol, risk_budget_pct=self.policy.legacy_risk_pct, protective_stop_price=stop, **common)]

    def on_position_closed(self, reason: str) -> None:
        self.strategy.on_position_closed(self.symbol, reason)
