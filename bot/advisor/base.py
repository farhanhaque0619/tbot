from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from bot.execution.market_state import MarketState

log = logging.getLogger(__name__)

REGIMES = ("trending", "mean_reverting", "high_volatility", "crisis", "unclear")
DIRECTIONS = ("long", "short", "neutral")
RISK_STATES = ("safe", "elevated", "unsafe")


@dataclass(frozen=True)
class MarketAdvice:
    regime: str = "unclear"
    setup_quality: int = 0           # 0-3
    direction: str = "neutral"
    risk_state: str = "safe"
    source: str = "null"
    latency_ms: float = 0.0
    error: str | None = None
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def validated(cls, d: dict[str, Any], *, source: str, latency_ms: float) -> MarketAdvice:
        """Coerce an untrusted dict into the closed vocabulary. Anything off-schema becomes 'unclear'/neutral."""
        regime = d.get("regime") if d.get("regime") in REGIMES else "unclear"
        direction = d.get("direction") if d.get("direction") in DIRECTIONS else "neutral"
        risk_state = d.get("risk_state") if d.get("risk_state") in RISK_STATES else "unsafe"
        try:
            q = int(d.get("setup_quality", 0))
        except (TypeError, ValueError):
            q = 0
        return cls(regime, max(0, min(3, q)), direction, risk_state, source, latency_ms, None, {k: d[k] for k in ("regime", "setup_quality", "direction", "risk_state") if k in d})


class Advisor(Protocol):
    name: str

    def advise(self, state: MarketState) -> MarketAdvice: ...


class NullAdvisor:
    name = "null"

    def advise(self, state: MarketState) -> MarketAdvice:
        return MarketAdvice(source=self.name)


class RuleAdvisor:
    """Deterministic baseline classifier over MarketState. Exists so that a model advisor has something to be
    compared against in calibration; it encodes no claim of edge."""
    name = "rule"

    def advise(self, state: MarketState) -> MarketAdvice:
        t0 = time.perf_counter()
        rvol, dist, dd = state.realized_vol, state.distance_from_ma, state.drawdown
        fast, slow = state.fast_ma, state.slow_ma
        if math.isfinite(rvol) and rvol > 0.5 or dd > 0.15:
            regime = "crisis"
        elif math.isfinite(rvol) and rvol > 0.30:
            regime = "high_volatility"
        elif math.isfinite(dist) and abs(dist) > 0.05:
            regime = "trending"
        elif math.isfinite(dist) and abs(dist) < 0.02:
            regime = "mean_reverting"
        else:
            regime = "unclear"
        if math.isfinite(fast) and math.isfinite(slow):
            direction = "long" if fast > slow else "short"
        else:
            direction = "neutral"
        risk_state = "unsafe" if regime == "crisis" else ("elevated" if regime == "high_volatility" or state.spread_bps > 20 else "safe")
        quality = 0
        if direction != "neutral":
            quality = 1 + (regime == "trending") + (risk_state == "safe")
        return MarketAdvice(regime, quality, direction, risk_state, self.name, (time.perf_counter() - t0) * 1e3)


def build_advisor(settings) -> Advisor | None:
    """Return the configured advisor or None. Only the flag decides; a misconfigured Jev never blocks trading."""
    if not getattr(settings, "enable_jev", False):
        return None
    from bot.advisor.jev import JevAdvisor
    return JevAdvisor(settings.jev_endpoint, settings.jev_api_key.get_secret_value())
