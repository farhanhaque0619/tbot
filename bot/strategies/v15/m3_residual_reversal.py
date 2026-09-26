"""M3: large-cap residual reversal on Tier 3 names (spec §5.5). PAPER-ONLY until promoted.

On the daily close, rank z = res5 / sigma_res_21d across eligible names (spread <= 5 bps when a quote is known, no
earnings within 5 sessions, synthetic-bar fraction < 0.2) and buy the bottom n (default 3): horizon 5 sessions,
volatility sigma_res_21d * sqrt(5), risk 0.25%, entry "opg" (whole shares) or "limit_at_prev_close_cancel_0945"
(fractional), exit "cls", overnight allowed, protective stop = ref * (1 - 3 * sigma_res_21d * sqrt(5)).
Exit when res5 > 0 on any close, after hold_sessions sessions, or when the close is below the fat-tail level.
Cross-sectional, so the engine calls ``on_event_batch`` with all symbols' snapshots at once.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, ClassVar

from bot.core.events import ScheduleEvent
from bot.core.intents import TradeIntent
from bot.features.engine import FeatureSnapshot
from bot.strategies.adapter import PositionView


class M3ResidualReversal:
    module_id: ClassVar[str] = "M3"
    default_params: ClassVar[dict[str, Any]] = {"n_bottom": 3, "hold_sessions": 5, "max_spread_bps": 5.0, "max_synth": 0.2, "fat_tail_mult": 3.0}

    def __init__(self, symbols, *, fractional: bool = False, **params):
        self.symbols = [s.upper() for s in symbols]
        self.fractional = fractional
        self.params = {**self.default_params, **params}
        self.listens = {"session_close"}
        self._entered: dict[str, tuple[date, float, float]] = {}      # symbol -> (decision session, ref price, sigma_res)
        self._sessions_held: dict[str, int] = {}

    def eligible(self, snap: FeatureSnapshot) -> bool:
        if snap.res5 is None or snap.sigma_res_21d is None or snap.sigma_res_21d <= 0 or snap.last_close is None:
            return False
        if snap.spread_bps is not None and snap.spread_bps > self.params["max_spread_bps"]:
            return False
        if snap.earnings_within_5:
            return False
        if snap.synthetic_frac_30m is not None and snap.synthetic_frac_30m >= self.params["max_synth"]:
            return False
        return True

    def on_event(self, event, snap, position) -> list[TradeIntent]:   # single-symbol path: no ranking possible
        return self.on_event_batch(event, {snap.symbol: snap}, {snap.symbol: position})

    def on_event_batch(self, event: ScheduleEvent, snaps: dict[str, FeatureSnapshot], positions: dict[str, PositionView]) -> list[TradeIntent]:
        if not isinstance(event, ScheduleEvent) or event.kind != "session_close":
            return []
        out: list[TradeIntent] = []
        held = {s for s, p in positions.items() if p.qty > 0}
        # exits first
        for s in sorted(held):
            snap = snaps.get(s)
            if snap is None or snap.last_close is None:
                continue
            self._sessions_held[s] = self._sessions_held.get(s, 0) + 1
            entered = self._entered.get(s)
            reason = None
            if snap.res5 is not None and snap.res5 > 0:
                reason = "res5>0"
            elif self._sessions_held[s] >= self.params["hold_sessions"]:
                reason = f"{self.params['hold_sessions']} sessions"
            elif entered is not None and snap.last_close < entered[1] * (1 - self.params["fat_tail_mult"] * entered[2] * math.sqrt(5)):
                reason = "fat tail"
            if reason:
                out.append(TradeIntent(s, self.module_id, event.ts, 0, 0, float(snap.last_close), 0.0, 0.0, 1.0, None, None, 0, "opg", "cls", True, None, reason))
                self._entered.pop(s, None)
                self._sessions_held.pop(s, None)
        for s in list(self._entered):
            if s not in held and (event.session_date - self._entered[s][0]).days > 10:
                self._entered.pop(s, None)         # never filled
        # entries: bottom n by z among eligible, not held, not exiting
        exiting = {i.symbol for i in out}
        cands = []
        for s, snap in snaps.items():
            if s in held or s in exiting or s in self._entered or not self.eligible(snap):
                continue
            cands.append((snap.res5 / snap.sigma_res_21d, s, snap))
        cands.sort(key=lambda t: (t[0], t[1]))
        for z, s, snap in cands[: int(self.params["n_bottom"])]:
            if z >= 0:
                break
            sig = float(snap.sigma_res_21d)
            ref = float(snap.last_close)
            self._entered[s] = (event.session_date, ref, sig)
            self._sessions_held[s] = 0
            out.append(TradeIntent(s, self.module_id, event.ts, +1, 5 * 86400, ref, sig * math.sqrt(5), 0.0025, min(abs(z) / 3.0, 1.0), None, None,
                                   5 * 86400, "limit_at_prev_close_cancel_0945" if self.fractional else "opg", "cls", True,
                                   ref * (1 - self.params["fat_tail_mult"] * sig * math.sqrt(5)), f"z={z:+.2f}"))
        return out
