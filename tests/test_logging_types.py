"""Regression: the secret-redaction filter must not change the type of numeric log arguments
(observed in real execution: 'cached %d bars' raised TypeError once real keys were configured)."""
import logging
from datetime import date


from bot.data.loader import BarLoader
from bot.data.store import BarStore
from bot.monitoring.logging import RedactFilter
from tests.conftest import make_bars
from tests.test_data_and_config import ScriptedProvider


def test_redact_filter_preserves_numeric_args_and_still_redacts_strings():
    f = RedactFilter(["supersecretvalue"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "cached %d bars for %s at %.2f (%s)", (11, "SPY", 3.5, "key=supersecretvalue"), None)
    assert f.filter(rec)
    assert rec.args[0] == 11 and isinstance(rec.args[0], int)
    assert isinstance(rec.args[2], float)
    assert rec.getMessage() == "cached 11 bars for SPY at 3.50 (key=***)"
    d = logging.LogRecord("x", logging.INFO, "", 0, "%(n)d rows %(k)s", {"n": 7, "k": "supersecretvalue"}, None)
    f.filter(d)
    assert d.args["n"] == 7 and d.getMessage() == "7 rows ***"
    e = logging.LogRecord("x", logging.INFO, "", 0, "evt", (), None)
    e.event = {"count": 3, "token": "supersecretvalue"}
    f.filter(e)
    assert e.event == {"count": 3, "token": "***"}


def test_upsert_bars_returns_int():
    s = BarStore()
    n = s.upsert_bars("SPY", make_bars(11))
    assert n == 11 and type(n) is int
    assert type(s.upsert_bars("SPY", make_bars(0).iloc[0:0])) is int


def test_loader_logs_cached_count_with_secrets_configured(caplog):
    """End-to-end: the exact code path that failed in production, with the redaction filter attached."""
    logger = logging.getLogger("bot.data.loader")
    flt = RedactFilter(["supersecretvalue"])
    logger.addFilter(flt)
    try:
        with caplog.at_level(logging.INFO, logger="bot.data.loader"):
            df = make_bars(60, seed=1)
            loader = BarLoader(BarStore(), ScriptedProvider(df))
            out = loader.get_daily("SPY", date(2020, 1, 15), date(2020, 2, 28))
        assert len(out) > 0
        msgs = [r.getMessage() for r in caplog.records]     # getMessage() is where the TypeError used to surface
        assert any(m.startswith("cached ") and " bars for SPY" in m for m in msgs), msgs
        cached = [r for r in caplog.records if r.msg.startswith("cached ")][0]
        assert isinstance(cached.args[0], int)
    finally:
        logger.removeFilter(flt)


def test_loader_warns_when_history_starts_later_than_requested(caplog):
    """Asking for 2015 from a source whose history begins in 2016 must be visible, not silent."""
    df = make_bars(300, seed=2, start="2016-01-04")
    loader = BarLoader(BarStore(), ScriptedProvider(df))
    with caplog.at_level(logging.WARNING, logger="bot.data.loader"):
        out = loader.get_daily("SPY", date(2015, 1, 1), date(2016, 6, 30))
    assert out.index[0].date() == date(2016, 1, 4)
    assert any("first bar returned is 2016-01-04" in r.getMessage() for r in caplog.records)
    # coverage recorded from the requested start: a second call does not refetch
    n_calls = len(loader.provider.calls)
    loader.get_daily("SPY", date(2015, 1, 1), date(2016, 6, 30))
    assert len(loader.provider.calls) == n_calls
