import os

import numpy as np
import pandas as pd
import pytest

NY = "America/New_York"


def make_bars(n: int = 600, seed: int = 0, start: str = "2020-01-01", drift: float = 0.0003, vol: float = 0.01,
              closes=None) -> pd.DataFrame:
    """Synthetic daily OHLCV, tz-aware NY index, business days."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, tz=NY)
    if closes is None:
        closes = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    closes = np.asarray(closes, dtype=float)
    opens = closes * (1 + rng.normal(0, 0.002, n))
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.003, n)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.003, n)))
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1e6}, index=idx)


@pytest.fixture
def bars() -> pd.DataFrame:
    return make_bars()


# ------------------------------------------------------------------ secrets never reach a test report
def _secret_values() -> list[str]:
    """Every environment value that looks like a credential (Alpaca keys, webhooks, tokens), longest first."""
    out = set()
    for k, v in os.environ.items():
        ku = k.upper()
        if v and len(v) >= 8 and any(t in ku for t in ("SECRET", "API_KEY", "TOKEN", "WEBHOOK", "PASSWORD")):
            out.add(v)
    try:
        from bot.config import Settings
        s = Settings()
        for attr in ("alpaca_paper_api_key", "alpaca_paper_secret_key", "alpaca_live_api_key", "alpaca_live_secret_key", "discord_webhook_url", "jev_api_key"):
            v = getattr(s, attr).get_secret_value()
            if v and len(v) >= 8:
                out.add(v)
    except Exception:  # noqa: BLE001
        pass
    return sorted(out, key=len, reverse=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Scrub credential values from failure reports: a long traceback prints request headers (observed 2026-10-18)."""
    outcome = yield
    rep = outcome.get_result()
    if rep.longrepr is None:
        return
    text = str(rep.longrepr)
    scrubbed = text
    for v in _secret_values():
        scrubbed = scrubbed.replace(v, "***REDACTED***")
    if scrubbed != text:
        rep.longrepr = scrubbed


def pytest_configure(config):
    # short tracebacks never print function-argument values (which include request headers for SDK calls)
    if getattr(config.option, "tbstyle", "auto") in ("auto", "long"):
        config.option.tbstyle = "short"
