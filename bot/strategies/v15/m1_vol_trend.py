"""M1: vol-managed index trend on SPY/QQQ (spec §5.3).

On the daily close: sigma = max(vol_21d, vol_63d); tsmom = ret_12m_ex_1m > 0; w = clip(target_vol / sigma, 0, cap) * tsmom.
Emit a target-weight intent on Mondays or when |w - w_actual| > rebalance_band; w = 0 (exit) when tsmom is false.
Entry style "cls" for whole-share accounts, "market_1555" for fractional. Protective stop = ref * (1 - 4 * sigma / sqrt(52)).
"""
from __future__ import annotations

import math
from typing import Any, ClassVar

from bot.core.events import ScheduleEvent
from bot.core.intents import TradeIntent
from bot.features.engine import FeatureSnapshot
from bot.strategies.adapter import PositionView


class M1VolTrend:
    module_id: ClassVar[str] = "M1"
    default_params: ClassVar[dict[str, Any]] = {"target_vol": 0.10, "cap": 0.60, "rebalance_band": 0.10, "stop_sigma_weeks": 4.0}

    def __init__(self, symbols=("SPY", "QQQ"), *, fractional: bool = False, **params):
        self.symbols = [s.upper() for s in symbols]
        self.fractional = fractional
        self.params = {**self.default_params, **params}
        self.listens = {"session_close"}

    # ------------------------------------------------------------------ rule
    def weight(self, snap: FeatureSnapshot) -> tuple[float | None, float | None]:
        """(w, sigma); None when the features are not ready (never guess)."""
        if snap.vol_d_21 is None or snap.vol_d_63 is None or snap.ret_12m_ex_1m is None:
            return None, None
        sigma = max(snap.vol_d_21, snap.vol_d_63)
        if sigma <= 0:
            return None, None
        tsmom = snap.ret_12m_ex_1m > 0
        w = min(max(self.params["target_vol"] / sigma, 0.0), self.params["cap"]) if tsmom else 0.0
        return w, sigma

    def on_event(self, event: ScheduleEvent, snap: FeatureSnapshot, position: PositionView) -> list[TradeIntent]:
        if not isinstance(event, ScheduleEvent) or event.kind != "session_close" or snap.last_close is None:
            return []
        w, sigma = self.weight(snap)
        if w is None:
            return []
        w_actual = position.weight if position.weight is not None else 0.0
        monday = event.session_date.weekday() == 0
        if w == 0.0:
            if position.qty > 0:
                return [self._intent(event, snap, 0, 0.0, sigma, "tsmom<=0 -> flat")]
            return []
        if not (monday or abs(w - w_actual) > self.params["rebalance_band"]):
            return []
        return [self._intent(event, snap, +1, w, sigma, f"w={w:.3f} sigma={sigma:.3f}")]

    def _intent(self, event, snap, direction, w, sigma, tag) -> TradeIntent:
        ref = float(snap.last_close)
        stop = ref * (1 - self.params["stop_sigma_weeks"] * sigma / math.sqrt(52)) if direction > 0 else None
        return TradeIntent(snap.symbol, self.module_id, event.ts, direction, 7 * 86400, ref, sigma, self.params["target_vol"],
                           min(w / self.params["cap"], 1.0) if direction > 0 else 0.0, None, None, 365 * 86400,
                           "market_1555" if self.fractional else "cls", "market_1555" if self.fractional else "cls", True, stop, tag,
                           target_weight=w if direction > 0 else None)
