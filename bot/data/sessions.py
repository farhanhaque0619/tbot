"""Session calendar with early closes (Phase 1).

Two sources, in priority order:
1. Broker calendar (`/v2/calendar`, incl. early closes), stored in the DuckDB `sessions` table by `sync_from_broker`.
2. Deterministic NYSE rules (holidays, 13:00 early closes, known special closures) as the offline fallback. These are
   exact for the regular NYSE schedule since 2016; unscheduled closures after the last entry in SPECIAL_CLOSURES are
   unknown offline, which is why the broker calendar is authoritative when available.

All datetimes are America/New_York. `cutoffs(date)` gives the documented Alpaca submission cutoffs: OPG before 09:28,
CLS before 15:50 (12:50 on an early close).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from bot.data.calendar import NY, SessionInfo

log = logging.getLogger(__name__)

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
OPG_CUTOFF = time(9, 28)
CLS_CUTOFF_MINUTES_BEFORE_CLOSE = 10

# Unscheduled full-day closures (date: reason). Extend when NYSE announces one.
SPECIAL_CLOSURES: dict[date, str] = {
    date(2018, 12, 5): "National day of mourning (G.H.W. Bush)",
    date(2025, 1, 9): "National day of mourning (J. Carter)",
}


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    """NYSE observance: Saturday -> Friday, Sunday -> Monday. Except New Year's on Saturday: no Friday holiday."""
    if d.weekday() == 5:
        return None if (d.month == 1 and d.day == 1) else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> dict[date, str]:
    out: dict[date, str] = {}

    def add(d: date | None, name: str):
        if d is not None:
            out[d] = name
    add(_observed(date(year, 1, 1)), "New Year's Day")
    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    add(_nth_weekday(year, 2, 0, 3), "Presidents' Day")
    add(easter(year) - timedelta(days=2), "Good Friday")
    add(_last_weekday(year, 5, 0), "Memorial Day")
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "Juneteenth")
    add(_observed(date(year, 7, 4)), "Independence Day")
    add(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    add(_observed(date(year, 12, 25)), "Christmas Day")
    for d, why in SPECIAL_CLOSURES.items():
        if d.year == year:
            out[d] = why
    return out


def nyse_early_closes(year: int) -> dict[date, str]:
    out: dict[date, str] = {}
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    out[thanksgiving + timedelta(days=1)] = "Day after Thanksgiving"
    j3 = date(year, 7, 3)
    if j3.weekday() <= 3 and j3 not in nyse_holidays(year):       # Mon-Thu, and not itself the observed holiday
        out[j3] = "Independence Day eve"
    d24 = date(year, 12, 24)
    if d24.weekday() <= 3 and d24 not in nyse_holidays(year):
        out[d24] = "Christmas Eve"
    return out


def rule_session(d: date) -> SessionInfo | None:
    """Session for a date from NYSE rules alone (None = closed)."""
    if d.weekday() >= 5 or d in nyse_holidays(d.year):
        return None
    close_t = EARLY_CLOSE if d in nyse_early_closes(d.year) else REGULAR_CLOSE
    return SessionInfo(d, datetime.combine(d, REGULAR_OPEN, NY), datetime.combine(d, close_t, NY), "rules")


@dataclass(frozen=True)
class Cutoffs:
    opg: datetime          # last moment an OPG order is accepted for this session's open
    cls: datetime          # last moment a CLS order is accepted for this session's close
    open: datetime
    close: datetime


class SessionCalendar:
    """Sessions by date. Broker-provided entries override rule-based ones."""

    def __init__(self, overrides: dict[date, SessionInfo | None] | None = None):
        self._overrides: dict[date, SessionInfo | None] = dict(overrides or {})

    # ---------------------------------------------------------------- sources
    @classmethod
    def from_sessions(cls, sessions: list[SessionInfo], start: date, end: date) -> SessionCalendar:
        """Broker calendar for [start, end]: every date in the range without an entry is a closure."""
        by = {s.date: s for s in sessions}
        overrides: dict[date, SessionInfo | None] = {}
        d = start
        while d <= end:
            overrides[d] = by.get(d)
            d += timedelta(days=1)
        return cls(overrides)

    @classmethod
    def from_store(cls, store) -> SessionCalendar:
        rows = store.con.execute("SELECT session_date, open_ts, close_ts, source FROM sessions ORDER BY session_date").fetchall()
        overrides: dict[date, SessionInfo | None] = {}
        for d, o, c, src in rows:
            if o is None:
                overrides[d] = None
            else:
                overrides[d] = SessionInfo(d, o.replace(tzinfo=NY), c.replace(tzinfo=NY), src)
        return cls(overrides)

    def sync_from_broker(self, broker, store, start: date, end: date) -> int:
        sessions = broker.get_sessions(start, end)
        cal = SessionCalendar.from_sessions(sessions, start, end)
        self._overrides.update(cal._overrides)
        rows = []
        for d, s in cal._overrides.items():
            rows.append((d, s.open.replace(tzinfo=None) if s else None, s.close.replace(tzinfo=None) if s else None, s.source if s else "closed"))
        store.con.executemany("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)", rows)
        return len(rows)

    # ---------------------------------------------------------------- queries
    def session(self, d: date) -> SessionInfo | None:
        if d in self._overrides:
            return self._overrides[d]
        return rule_session(d)

    def is_session(self, d: date) -> bool:
        return self.session(d) is not None

    def session_bounds(self, d: date) -> tuple[datetime, datetime] | None:
        s = self.session(d)
        return (s.open, s.close) if s else None

    def is_regular_hours(self, ts: datetime) -> bool:
        ts = ts.astimezone(NY)
        s = self.session(ts.date())
        return bool(s and s.open <= ts < s.close)

    def is_early_close(self, d: date) -> bool:
        s = self.session(d)
        return bool(s and s.close.time() < REGULAR_CLOSE)

    def cutoffs(self, d: date) -> Cutoffs | None:
        s = self.session(d)
        if s is None:
            return None
        return Cutoffs(opg=datetime.combine(d, OPG_CUTOFF, NY), cls=s.close - timedelta(minutes=CLS_CUTOFF_MINUTES_BEFORE_CLOSE),
                       open=s.open, close=s.close)

    def sessions_between(self, start: date, end: date) -> list[SessionInfo]:
        out, d = [], start
        while d <= end:
            s = self.session(d)
            if s:
                out.append(s)
            d += timedelta(days=1)
        return out

    def next_session(self, d: date) -> SessionInfo:
        d += timedelta(days=1)
        for _ in range(15):
            s = self.session(d)
            if s:
                return s
            d += timedelta(days=1)
        raise RuntimeError("no session within 15 days")

    def prev_session(self, d: date) -> SessionInfo:
        d -= timedelta(days=1)
        for _ in range(15):
            s = self.session(d)
            if s:
                return s
            d -= timedelta(days=1)
        raise RuntimeError("no session within 15 days")

    def last_completed_session(self, now: datetime) -> SessionInfo:
        now = now.astimezone(NY)
        s = self.session(now.date())
        if s and s.close <= now:
            return s
        return self.prev_session(now.date())

    def expected_minutes(self, d: date) -> int:
        s = self.session(d)
        return int((s.close - s.open).total_seconds() // 60) if s else 0
