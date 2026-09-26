from datetime import date, datetime, time, timedelta

import pytest

from bot.data.calendar import NY, SessionInfo
from bot.data.sessions import SessionCalendar, easter, nyse_early_closes, nyse_holidays, rule_session
from bot.data.store import BarStore


def test_easter_and_good_friday():
    assert easter(2024) == date(2024, 3, 31) and easter(2025) == date(2025, 4, 20) and easter(2026) == date(2026, 4, 5)
    assert date(2026, 4, 3) in nyse_holidays(2026)      # Good Friday 2026


@pytest.mark.parametrize("d, closed", [
    (date(2024, 6, 19), True),    # Juneteenth (Wednesday)
    (date(2021, 6, 18), False),   # Juneteenth not yet observed by NYSE in 2021
    (date(2025, 1, 9), True),     # Carter national day of mourning
    (date(2018, 12, 5), True),    # Bush
    (date(2022, 1, 17), True),    # MLK
    (date(2022, 12, 26), True),   # Christmas observed (Dec 25 Sunday)
    (date(2021, 12, 24), True),   # Christmas observed (Dec 25 Saturday)
    (date(2021, 12, 31), False),  # New Year's Day 2022 is Saturday: NO Friday holiday
    (date(2023, 1, 2), True),     # New Year's observed (Jan 1 Sunday)
    (date(2026, 7, 3), True),     # July 4 2026 is Saturday -> Friday closed
    (date(2026, 9, 25), False),
    (date(2026, 9, 26), True),    # Saturday
    (date(2024, 11, 28), True),   # Thanksgiving
])
def test_holiday_rules(d, closed):
    assert (rule_session(d) is None) == closed


@pytest.mark.parametrize("d, early", [
    (date(2024, 11, 29), True),   # day after Thanksgiving
    (date(2024, 12, 24), True),   # Christmas Eve, Tuesday
    (date(2023, 12, 22), False),  # Friday before Christmas Monday: full day
    (date(2024, 7, 3), True),     # Wednesday July 3
    (date(2022, 7, 1), False),    # July 4 2022 Monday: no early close on Friday
    (date(2025, 7, 3), True),     # Thursday July 3 2025
])
def test_early_close_rules(d, early):
    s = rule_session(d)
    assert s is not None
    assert (s.close.time() == time(13, 0)) == early
    assert (d in nyse_early_closes(d.year)) == early


def test_cutoffs_and_expected_minutes():
    cal = SessionCalendar()
    c = cal.cutoffs(date(2026, 9, 25))
    assert c.opg == datetime(2026, 9, 25, 9, 28, tzinfo=NY) and c.cls == datetime(2026, 9, 25, 15, 50, tzinfo=NY)
    e = cal.cutoffs(date(2024, 11, 29))
    assert e.cls == datetime(2024, 11, 29, 12, 50, tzinfo=NY) and cal.is_early_close(date(2024, 11, 29))
    assert cal.expected_minutes(date(2026, 9, 25)) == 390 and cal.expected_minutes(date(2024, 11, 29)) == 210
    assert cal.cutoffs(date(2026, 9, 26)) is None


def test_dst_transition_sessions_have_correct_utc_offsets():
    cal = SessionCalendar()
    march = cal.session(date(2026, 3, 9))       # first session after clocks go forward (2026-03-08)
    nov = cal.session(date(2026, 11, 2))        # first session after clocks go back (2026-11-01)
    assert march.open.utcoffset() == timedelta(hours=-4) and nov.open.utcoffset() == timedelta(hours=-5)
    assert (march.close - march.open) == timedelta(hours=6, minutes=30) == (nov.close - nov.open)
    assert cal.is_regular_hours(datetime(2026, 3, 9, 9, 30, tzinfo=NY)) and not cal.is_regular_hours(datetime(2026, 3, 9, 16, 0, tzinfo=NY))


def test_broker_calendar_overrides_rules_and_persists():
    store = BarStore()
    # broker says: 2026-10-05 (a normal Monday) is closed, 2026-10-06 closes early at 13:00
    sessions = [SessionInfo(date(2026, 10, 6), datetime(2026, 10, 6, 9, 30, tzinfo=NY), datetime(2026, 10, 6, 13, 0, tzinfo=NY), "alpaca"),
                SessionInfo(date(2026, 10, 7), datetime(2026, 10, 7, 9, 30, tzinfo=NY), datetime(2026, 10, 7, 16, 0, tzinfo=NY), "alpaca")]

    class B:
        def get_sessions(self, a, b):
            return sessions
    cal = SessionCalendar()
    assert cal.is_session(date(2026, 10, 5))
    n = cal.sync_from_broker(B(), store, date(2026, 10, 5), date(2026, 10, 7))
    assert n == 3 and not cal.is_session(date(2026, 10, 5)) and cal.is_early_close(date(2026, 10, 6))
    cal2 = SessionCalendar.from_store(store)
    assert not cal2.is_session(date(2026, 10, 5)) and cal2.session(date(2026, 10, 6)).close.time() == time(13, 0)
    assert cal2.session(date(2026, 10, 8)).source == "rules"       # outside the synced range: rules apply
    assert cal2.next_session(date(2026, 10, 4)).date == date(2026, 10, 6)
    assert cal2.prev_session(date(2026, 10, 6)).date == date(2026, 10, 2)
    assert cal2.last_completed_session(datetime(2026, 10, 6, 13, 30, tzinfo=NY)).date == date(2026, 10, 6)
    assert cal2.last_completed_session(datetime(2026, 10, 6, 12, 30, tzinfo=NY)).date == date(2026, 10, 2)
