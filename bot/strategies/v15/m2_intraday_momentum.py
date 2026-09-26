"""M2: market intraday momentum on SPY/QQQ (spec §5.4).

At entry_time (default 15:30): s = sign(r1) if |r1| > k * sigma_last30 else 0 (k = 0.5). Optional research variants:
agreement filter sign(r1) == sign(r12) (r12 is only known from 15:30 on; an earlier entry time with the filter never
trades) and relvol_1515 > 1.0. Intent: direction s, horizon 1800 s, volatility sigma_last30, risk 0.25%,
marketable_limit entry, exit "cls" at t1550 for whole-share accounts or "market_1558" at t1558 for fractional, no
overnight, no broker stop. Software fat-tail guard: on 1-minute bars an adverse move of fat_tail_mult * sigma_last30
from the slice's average price exits at market. Shorts are emitted as s = -1; the policy decides whether they trade.
"""
from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any, ClassVar

from bot.core.events import ScheduleEvent
from bot.core.intents import TradeIntent
from bot.features.engine import FeatureSnapshot
from bot.strategies.adapter import PositionView


class M2IntradayMomentum:
    module_id: ClassVar[str] = "M2"
    default_params: ClassVar[dict[str, Any]] = {"k": 0.5, "agreement": False, "relvol_filter": False, "entry_time": "15:30", "fat_tail_mult": 3.0}

    def __init__(self, symbols=("SPY", "QQQ"), *, fractional: bool = False, **params):
        self.symbols = [s.upper() for s in symbols]
        self.fractional = fractional
        self.params = {**self.default_params, **params}
        hh, mm = str(self.params["entry_time"]).split(":")
        self.entry_time = time(int(hh), int(mm))
        self.exit_kind = "t1558" if fractional else "t1550"
        self.listens = {"bar_close_1m", self.exit_kind}
        self._traded: dict[str, date] = {}          # symbol -> session in which we already traded
        self._exit_sent: dict[str, date] = {}       # symbol -> session in which an exit was already emitted
        self._sigma: dict[str, float] = {}

    # ------------------------------------------------------------------ rule
    def signal(self, snap: FeatureSnapshot) -> int:
        if snap.r1 is None or snap.sigma_last30 is None or snap.sigma_last30 <= 0:
            return 0
        if abs(snap.r1) <= self.params["k"] * snap.sigma_last30:
            return 0
        s = 1 if snap.r1 > 0 else -1
        if self.params["agreement"]:
            if snap.r12 is None or (1 if snap.r12 > 0 else -1) != s:
                return 0
        if self.params["relvol_filter"] and not (snap.relvol_1515 is not None and snap.relvol_1515 > 1.0):
            return 0
        return s

    def on_event(self, event: ScheduleEvent, snap: FeatureSnapshot, position: PositionView) -> list[TradeIntent]:
        if not isinstance(event, ScheduleEvent) or snap.last_close is None:
            return []
        sym, d = snap.symbol, event.session_date
        if event.kind == self.exit_kind:
            if position.qty != 0 and self._exit_sent.get(sym) != d:
                self._exit_sent[sym] = d
                return [self._exit(event, snap, "market_1558" if self.fractional else "cls", "time exit")]
            return []
        if event.kind != "bar_close" or event.timeframe != "1m":
            return []
        wall = (event.ts + timedelta(minutes=1)).time()
        # fat-tail guard on every 1-minute bar while in a position
        if position.qty != 0 and position.avg_price:
            sigma = self._sigma.get(sym) or snap.sigma_last30
            if sigma:
                side = 1 if position.qty > 0 else -1
                move = side * (snap.last_close / position.avg_price - 1)
                if move < -self.params["fat_tail_mult"] * sigma and self._exit_sent.get(sym) != d:
                    self._exit_sent[sym] = d
                    return [self._exit(event, snap, "market", f"fat tail {move:+.4f} < -{self.params['fat_tail_mult']}x{sigma:.4f}")]
            return []
        if wall != self.entry_time or self._traded.get(sym) == d or position.qty != 0:
            return []
        s = self.signal(snap)
        if s == 0:
            return []
        self._traded[sym] = d
        self._sigma[sym] = float(snap.sigma_last30)
        return [TradeIntent(sym, self.module_id, event.ts, s, 1800, float(snap.last_close), float(snap.sigma_last30), 0.0025,
                            min(abs(snap.r1) / (self.params["k"] * snap.sigma_last30) / 4.0, 1.0), None, None, 1800, "marketable_limit",
                            "market_1558" if self.fractional else "cls", False, None, f"r1={snap.r1:+.4f} k*sigma={self.params['k'] * snap.sigma_last30:.4f}")]

    def _exit(self, event, snap, style, tag) -> TradeIntent:
        return TradeIntent(snap.symbol, self.module_id, event.ts, 0, 0, float(snap.last_close), 0.0, 0.0, 1.0, None, None, 0, "market", style, False, None, tag)
