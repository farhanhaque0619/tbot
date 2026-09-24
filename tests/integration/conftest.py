"""Integration tests against the REAL Alpaca PAPER API.

They never run by default. Enable with:

    RUN_ALPACA_INTEGRATION=1 python -m pytest tests/integration -q -s

and paper credentials in the environment / .env. Tests that SUBMIT paper orders additionally require
RUN_ALPACA_INTEGRATION_ORDERS=1. Nothing here ever touches the live environment: the broker is constructed with
env="paper" explicitly and the tests assert the account number is paper-shaped before doing anything else.
"""
import os

import pytest

from bot.config import Settings


def _enabled() -> bool:
    return os.environ.get("RUN_ALPACA_INTEGRATION") == "1"


@pytest.fixture(scope="session")
def settings():
    s = Settings()
    if not _enabled():
        pytest.skip("RUN_ALPACA_INTEGRATION=1 not set")
    if not s.has_credentials("paper"):
        pytest.skip("paper credentials not configured")
    return s


@pytest.fixture(scope="session")
def broker(settings):
    from bot.execution.broker import AlpacaBroker
    b = AlpacaBroker.for_env(settings, "paper")
    ok, why = b.verify_account_env()
    assert ok, f"refusing to run integration tests: {why}"
    assert b.is_paper and "paper-api" in b.base_url
    return b


@pytest.fixture(scope="session")
def orders_enabled():
    if os.environ.get("RUN_ALPACA_INTEGRATION_ORDERS") != "1":
        pytest.skip("RUN_ALPACA_INTEGRATION_ORDERS=1 not set (paper order submission tests)")
    return True
