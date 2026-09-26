"""Doctor health semantics: OK / DEGRADED / FAILED, and the account probe against the fake broker."""
from datetime import date, timedelta

import pandas as pd
import pytest

from bot.monitoring.health import DEGRADED, FAILED, OK, HealthReport, classify_bar_currency, classify_quote_age


def test_report_levels_and_exit_codes():
    r = HealthReport()
    assert r.level == OK and r.exit_code == 0
    r.add("a", OK)
    r.add("b", DEGRADED, "meh")
    assert r.level == DEGRADED and r.exit_code == 2 and [c.name for c in r.problems()] == ["b"]
    r.add("c", FAILED, "boom", required=False)          # optional failure only degrades
    assert r.level == DEGRADED
    r.add("d", FAILED, "required boom")
    assert r.level == FAILED and r.exit_code == 1
    with pytest.raises(ValueError):
        r.add("e", "MAYBE")


def test_quote_age_semantics():
    assert classify_quote_age(60 * 60 * 40, market_open=False, max_stale_seconds=900)[0] == OK      # weekend: fine
    assert classify_quote_age(30, market_open=True, max_stale_seconds=900)[0] == OK
    assert classify_quote_age(2000, market_open=True, max_stale_seconds=900)[0] == DEGRADED
    assert classify_quote_age(None, market_open=True, max_stale_seconds=900)[0] == DEGRADED


def test_bar_currency_semantics():
    s = date(2026, 9, 25)
    assert classify_bar_currency(s, s)[0] == OK
    assert classify_bar_currency(s - timedelta(days=1), s)[0] == DEGRADED
    assert classify_bar_currency(s + timedelta(days=1), s)[0] == DEGRADED
    assert classify_bar_currency(None, s)[0] == FAILED


def _doctor_probe(tmp_path, *, bars_end: str, break_calendar: bool = False, quote_age_s: float = 5.0, market_open: bool | None = None):
    from bot.cli import _account_table
    from bot.config import Settings
    from bot.data.calendar import NY
    from bot.data.store import BarStore
    from bot.execution import FakeBroker
    from bot.execution.fake_broker import FakeAPIError
    from bot.data.calendar import now_ny
    from tests.conftest import make_bars

    settings = Settings(_env_file=None, data_db_path=tmp_path / "bars.duckdb", state_dir=tmp_path, log_dir=tmp_path,
                        alpaca_paper_api_key="PKTESTPLACEHOLDER", alpaca_paper_secret_key="placeholder-secret-value")
    df = make_bars(60, seed=1)
    df.index = pd.bdate_range(end=bars_end, periods=60, tz=NY)
    store = BarStore(settings.data_db_path)
    store.upsert_bars("SPY", df)
    store.set_coverage("SPY", date(2000, 1, 1), date(2100, 1, 1))
    store.close()
    broker = FakeBroker(cash=100.0, prices={"SPY": float(df["close"].iloc[-1])})
    broker.now = now_ny()
    broker.quote_age = timedelta(seconds=quote_age_s)
    if market_open is not None:
        broker.market_open_override = market_open
    if break_calendar:
        broker.get_sessions = lambda a, b: (_ for _ in ()).throw(FakeAPIError("calendar exploded", 500))
    _t, info, rep = _account_table("paper", settings, broker, "SPY")
    return info, rep


def test_doctor_probe_is_ok_when_last_session_bar_is_cached(tmp_path):
    from bot.data.calendar import last_completed_session_date, now_ny
    sess = last_completed_session_date(now_ny())
    info, rep = _doctor_probe(tmp_path, bars_end=sess.isoformat(), market_open=False, quote_age_s=3 * 24 * 3600)
    assert rep.level == OK, [(c.name, c.level, c.detail) for c in rep.problems()]
    assert info["bar_current"] is True


def test_doctor_probe_degrades_when_bars_lag(tmp_path):
    info, rep = _doctor_probe(tmp_path, bars_end="2020-01-10", market_open=False)
    assert rep.level == DEGRADED and info["bar_current"] is False
    assert any(c.name == "daily_bars" and c.level == DEGRADED for c in rep.checks)


def test_doctor_probe_fails_when_calendar_errors(tmp_path):
    info, rep = _doctor_probe(tmp_path, bars_end="2020-01-10", break_calendar=True)
    assert rep.level == FAILED
    assert any(c.name == "calendar" and c.level == FAILED for c in rep.checks)


def test_doctor_probe_stale_quote_while_open_is_degraded_not_failed(tmp_path):
    from bot.data.calendar import last_completed_session_date, now_ny
    sess = last_completed_session_date(now_ny())
    info, rep = _doctor_probe(tmp_path, bars_end=sess.isoformat(), market_open=True, quote_age_s=5000)
    assert rep.level == DEGRADED
    assert [c.name for c in rep.problems()] == ["quote_freshness"]
