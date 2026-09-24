"""Jev adapter (Phase 10). Behind ENABLE_JEV=false. Generic JSON-over-HTTPS contract:

    POST {JEV_ENDPOINT}   Authorization: Bearer <JEV_API_KEY>
    body: {"task": "classify_market_state", "schema": {...closed vocabulary...}, "state": MarketState.to_dict()}
    response: {"regime": ..., "setup_quality": 0-3, "direction": ..., "risk_state": ...}

The actual Jev API may differ; adapt ``_payload`` / ``_parse`` here and nowhere else. The advisor never raises:
any failure yields an 'unclear'/neutral/unsafe advice with ``error`` set, and shadow mode ignores it anyway.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from bot.advisor.base import DIRECTIONS, REGIMES, RISK_STATES, MarketAdvice
from bot.execution.market_state import MarketState

log = logging.getLogger(__name__)


class JevAdvisor:
    name = "jev"

    def __init__(self, endpoint: str, api_key: str, *, timeout: float = 5.0, session: requests.Session | None = None):
        self.endpoint = endpoint
        self._key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def _payload(self, state: MarketState) -> dict[str, Any]:
        return {"task": "classify_market_state",
                "schema": {"regime": list(REGIMES), "setup_quality": [0, 1, 2, 3], "direction": list(DIRECTIONS), "risk_state": list(RISK_STATES)},
                "state": state.to_dict()}

    @staticmethod
    def _parse(body: Any) -> dict[str, Any]:
        if isinstance(body, dict) and isinstance(body.get("result"), dict):
            body = body["result"]
        return body if isinstance(body, dict) else {}

    def advise(self, state: MarketState) -> MarketAdvice:
        t0 = time.perf_counter()
        if not self.endpoint:
            return MarketAdvice(source=self.name, error="JEV_ENDPOINT not configured", risk_state="unsafe")
        try:
            r = self.session.post(self.endpoint, json=self._payload(state), timeout=self.timeout,
                                  headers={"Authorization": f"Bearer {self._key}"} if self._key else {})
            r.raise_for_status()
            return MarketAdvice.validated(self._parse(r.json()), source=self.name, latency_ms=(time.perf_counter() - t0) * 1e3)
        except Exception as e:  # noqa: BLE001 - advisory only, never fatal
            log.warning("jev advisor failed: %s", type(e).__name__)
            return MarketAdvice(source=self.name, error=f"{type(e).__name__}", risk_state="unsafe",
                                latency_ms=(time.perf_counter() - t0) * 1e3)
