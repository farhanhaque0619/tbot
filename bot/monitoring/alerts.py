"""Optional Discord webhook alerts. Never raises - a dead webhook must not stop trading."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, webhook_url: str = "", *, timeout: float = 5.0):
        self.webhook_url = webhook_url or ""
        self.timeout = timeout
        self.sent: list[str] = []  # for tests / dashboard

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send(self, title: str, message: str = "", *, level: str = "info") -> bool:
        text = f"{'🚨 ' if level == 'critical' else '⚠️ ' if level == 'warning' else ''}**{title}**\n{message}".strip()
        self.sent.append(text)
        if not self.enabled:
            return False
        try:
            r = requests.post(self.webhook_url, json={"content": text[:1900]}, timeout=self.timeout)
            if r.status_code >= 400:
                log.warning("discord webhook returned %s", r.status_code)
                return False
            return True
        except requests.RequestException as e:  # noqa: PERF203
            log.warning("discord webhook failed: %s", type(e).__name__)
            return False
