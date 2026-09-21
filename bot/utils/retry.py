"""Retry with exponential backoff for API calls.

Alpaca's SDK already retries HTTP 429 internally a few times; this wrapper adds
backoff for transient server errors and network failures, and re-tries 429s
that survived the SDK's own retries.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Iterable, TypeVar

import requests

log = logging.getLogger(__name__)
T = TypeVar("T")

# HTTP status codes worth retrying. 4xx other than 429 are caller bugs and are not retried.
RETRY_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _status_of(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an alpaca/requests exception."""
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) is not None:
        return int(resp.status_code)
    return None


def is_retryable(exc: BaseException, extra_types: Iterable[type] = ()) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, tuple(extra_types)):
        return True
    sc = _status_of(exc)
    return sc in RETRY_STATUS


def with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    what: str = "api call",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` with exponential backoff + jitter on retryable errors.

    Non-retryable errors are raised immediately. After ``attempts`` failures
    the last error is raised.
    """
    delay = base_delay
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we classify below
            if not is_retryable(exc) or i == attempts:
                raise
            jitter = random.uniform(0, delay * 0.25)
            wait = min(max_delay, delay + jitter)
            log.warning("%s failed (attempt %d/%d, %s: %s); retrying in %.1fs",
                        what, i, attempts, type(exc).__name__, _status_of(exc) or exc, wait)
            sleep(wait)
            delay = min(max_delay, delay * 2)
    raise RuntimeError("unreachable")  # pragma: no cover
