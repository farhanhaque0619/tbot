"""Market-time helpers. Everything user-facing is America/New_York.

Alpaca's clock/calendar endpoints are the source of truth when online; the
offline fallback (weekday 09:30-16:00 ET, no holiday table) is only used when
no broker is available and is clearly marked as such.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from bot.config import NY_TZ

NY = ZoneInfo(NY_TZ)
UTC = ZoneInfo("UTC")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
log = logging.getLogger(__name__)


def now_ny() -> datetime:
    return datetime.now(tz=NY)


def to_ny(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC").tz_convert(NY_TZ) if ts.tzinfo is None else ts.tz_convert(NY_TZ)


def to_ny_index(idx: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx).as_unit("ns")  # DuckDB hands back microseconds; keep one unit everywhere
    # Use the tz *name* so we get the same tz implementation pandas picks for string zones.
    return idx.tz_localize("UTC").tz_convert(NY_TZ) if idx.tz is None else idx.tz_convert(NY_TZ)


def daily_ts(d: date) -> pd.Timestamp:
    """Canonical timestamp for a daily bar: midnight New York time on that date."""
    return pd.Timestamp(datetime.combine(d, time(0, 0))).tz_localize(NY_TZ)


def normalize_session_time(session_date: date, value: datetime | time) -> datetime:
    """Normalise an Alpaca calendar ``open``/``close`` value to an aware America/New_York datetime.

    Accepted inputs and their interpretation:
    - ``datetime.time``: combined with ``session_date`` in New York time.
    - naive ``datetime.datetime``: interpreted as New York wall-clock time. This is what alpaca-py produces:
      the /v2/calendar API returns ``"open": "09:30"`` (exchange local time, America/New_York) and
      ``alpaca.trading.models.Calendar.__init__`` parses ``f"{date} {open}"`` with ``strptime("%Y-%m-%d %H:%M")``
      into a NAIVE datetime. Its date part is trusted only if it matches ``session_date``; otherwise the time
      part is re-attached to ``session_date`` (the calendar entry's date is authoritative).
    - aware ``datetime.datetime``: converted with ``astimezone(NY)``; tzinfo is never blindly replaced.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(NY)
        if value.date() != session_date:
            log.warning("calendar value %s does not match session date %s; using the session date", value, session_date)
            return datetime.combine(session_date, value.time(), NY)
        return value.replace(tzinfo=NY)
    if isinstance(value, time):
        return datetime.combine(session_date, value.replace(tzinfo=None), NY)
    raise TypeError(f"unsupported calendar time value {value!r} ({type(value).__name__})")


@dataclass(frozen=True)
class SessionInfo:
    date: date
    open: datetime
    close: datetime
    source: str  # "alpaca" | "fallback"


def fallback_session(d: date) -> SessionInfo | None:
    """Weekday-only heuristic. Ignores exchange holidays (documented limitation)."""
    if d.weekday() >= 5:
        return None
    return SessionInfo(d, datetime.combine(d, MARKET_OPEN, NY), datetime.combine(d, MARKET_CLOSE, NY), "fallback")


def last_completed_session_date(now: datetime | None = None, sessions: list[SessionInfo] | None = None) -> date:
    """The most recent trading day whose close is in the past.

    If ``sessions`` (from the broker calendar) is given it is authoritative;
    otherwise use the weekday fallback.
    """
    now = now or now_ny()
    now = now.astimezone(NY)
    if sessions:
        done = [s for s in sessions if s.close <= now]
        if done:
            return max(done, key=lambda s: s.date).date
        # No session in the window has closed yet; fall through to heuristic.
    d = now.date()
    for _ in range(10):
        s = fallback_session(d)
        if s and s.close <= now:
            return d
        d -= timedelta(days=1)
    raise RuntimeError("could not determine last completed session")


def is_opg_window(now: datetime | None = None) -> bool:
    """Alpaca accepts market-on-open (OPG) orders from 19:00 ET until 09:28 ET the next day."""
    now = (now or now_ny()).astimezone(NY)
    t = now.time()
    return t >= time(19, 0) or t < time(9, 28)
