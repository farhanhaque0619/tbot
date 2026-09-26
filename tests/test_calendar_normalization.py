"""Regression tests for Alpaca calendar open/close normalisation (real SDK returns NAIVE datetimes, not times)."""
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.data.calendar import NY, last_completed_session_date, normalize_session_time
from bot.execution.broker import AlpacaBroker

TOKYO = ZoneInfo("Asia/Tokyo")
D = date(2026, 9, 25)          # a Friday, EDT (UTC-4)
D_WINTER = date(2026, 1, 15)   # EST (UTC-5)


def _assert_ny(dt: datetime, d: date, hh: int, mm: int):
    assert dt.tzinfo is not None and dt.utcoffset() is not None
    assert dt.tzinfo.key == "America/New_York" if hasattr(dt.tzinfo, "key") else str(dt.tzinfo) == "America/New_York"
    assert (dt.date(), dt.hour, dt.minute) == (d, hh, mm)


@pytest.mark.parametrize("field, hh, mm", [("open", 9, 30), ("close", 16, 0)])
class TestNormalizeSessionTime:
    def test_time_input(self, field, hh, mm):
        _assert_ny(normalize_session_time(D, time(hh, mm)), D, hh, mm)

    def test_time_input_with_tzinfo_is_reattached_to_ny(self, field, hh, mm):
        _assert_ny(normalize_session_time(D, time(hh, mm, tzinfo=timezone.utc)), D, hh, mm)

    def test_naive_datetime_is_new_york_wall_clock(self, field, hh, mm):
        # exactly what alpaca-py's Calendar.__init__ produces from "09:30" / "16:00"
        out = normalize_session_time(D, datetime(2026, 9, 25, hh, mm))
        _assert_ny(out, D, hh, mm)
        assert out.utcoffset() == timedelta(hours=-4)
        out_w = normalize_session_time(D_WINTER, datetime(2026, 1, 15, hh, mm))
        _assert_ny(out_w, D_WINTER, hh, mm)
        assert out_w.utcoffset() == timedelta(hours=-5)

    def test_aware_utc_datetime_is_converted_not_relabelled(self, field, hh, mm):
        utc = datetime(2026, 9, 25, hh + 4, mm, tzinfo=timezone.utc)   # 13:30Z == 09:30 EDT
        out = normalize_session_time(D, utc)
        _assert_ny(out, D, hh, mm)
        assert out == utc

    def test_aware_other_zone_datetime(self, field, hh, mm):
        tokyo = datetime(2026, 9, 25, hh, mm, tzinfo=NY).astimezone(TOKYO)
        assert tokyo.date() == date(2026, 9, 25) or tokyo.date() == date(2026, 9, 26)
        out = normalize_session_time(D, tokyo)
        _assert_ny(out, D, hh, mm)

    def test_aware_ny_datetime_unchanged(self, field, hh, mm):
        src = datetime(2026, 9, 25, hh, mm, tzinfo=NY)
        assert normalize_session_time(D, src) == src


def test_naive_datetime_with_wrong_date_uses_session_date():
    out = normalize_session_time(D, datetime(2026, 9, 24, 9, 30))
    _assert_ny(out, D, 9, 30)


def test_unsupported_type_raises():
    with pytest.raises(TypeError):
        normalize_session_time(D, "09:30")   # type: ignore[arg-type]


def test_session_from_real_sdk_calendar_model():
    """Construct the alpaca-py Calendar exactly as the API payload does; open/close come back as naive datetimes."""
    from alpaca.trading.models import Calendar
    cal = Calendar(date="2026-09-25", open="09:30", close="16:00")
    assert isinstance(cal.open, datetime) and cal.open.tzinfo is None, "SDK contract this fix relies on"
    s = AlpacaBroker.session_from_calendar(cal)
    assert s.date == D and s.source == "alpaca"
    _assert_ny(s.open, D, 9, 30)
    _assert_ny(s.close, D, 16, 0)
    # an early close (e.g. day after Thanksgiving) is respected
    s2 = AlpacaBroker.session_from_calendar(Calendar(date="2026-11-27", open="09:30", close="13:00"))
    assert s2.close.hour == 13


@pytest.mark.parametrize("open_, close_", [
    (time(9, 30), time(16, 0)),
    (datetime(2026, 9, 25, 9, 30), datetime(2026, 9, 25, 16, 0)),
    (datetime(2026, 9, 25, 13, 30, tzinfo=timezone.utc), datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)),
    (datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY)),
])
def test_session_from_calendar_accepts_all_shapes(open_, close_):
    s = AlpacaBroker.session_from_calendar(SimpleNamespace(date=D, open=open_, close=close_))
    _assert_ny(s.open, D, 9, 30)
    _assert_ny(s.close, D, 16, 0)
    # and downstream logic works: at 17:00 NY the session is complete, at 12:00 it is not
    assert last_completed_session_date(datetime(2026, 9, 25, 17, 0, tzinfo=NY), [s]) == D
    assert last_completed_session_date(datetime(2026, 9, 25, 12, 0, tzinfo=NY), [s]) == D - timedelta(days=1)


def test_session_from_calendar_rejects_close_before_open():
    with pytest.raises(ValueError):
        AlpacaBroker.session_from_calendar(SimpleNamespace(date=D, open=time(16, 0), close=time(9, 30)))


def test_string_date_is_accepted():
    s = AlpacaBroker.session_from_calendar(SimpleNamespace(date="2026-09-25", open=time(9, 30), close=time(16, 0)))
    assert s.date == D
