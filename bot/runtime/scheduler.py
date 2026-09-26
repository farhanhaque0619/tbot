"""Session scheduler: the fixed ScheduleEvents of a trading session, relative to its official close so early closes
get the same marks (spec §12). ``due(now)`` returns every event whose time has passed and not yet fired (idempotent)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from bot.core.events import ScheduleEvent
from bot.data.calendar import NY, SessionInfo

PRE_OPEN = time(9, 0)
PROTECT = time(9, 29)          # fractional DAY stops are re-placed here (spec §8)
EVENING = time(19, 5)          # after-hours order window opens at 19:00 ET: next-day OPG/CLS orders can be placed


@dataclass
class SessionSchedule:
    session: SessionInfo
    events: list[ScheduleEvent] = field(default_factory=list)

    @classmethod
    def build(cls, session: SessionInfo, *, summary_after_close_minutes: int = 30) -> "SessionSchedule":
        d, c = session.date, session.close
        ev = [ScheduleEvent("pre_open", datetime.combine(d, PRE_OPEN, NY), d),
              ScheduleEvent("pre_open", datetime.combine(d, PROTECT, NY), d, "protect"),
              ScheduleEvent("session_open", session.open, d),
              ScheduleEvent("t1530", c - timedelta(minutes=30), d),
              ScheduleEvent("t1550", c - timedelta(minutes=10), d),
              ScheduleEvent("t1558", c - timedelta(minutes=2), d),
              ScheduleEvent("session_close", c, d),
              ScheduleEvent("post_close", c + timedelta(minutes=summary_after_close_minutes), d),
              ScheduleEvent("evening", datetime.combine(d, EVENING, NY), d)]
        return cls(session, sorted(ev, key=lambda e: e.ts))


class Scheduler:
    def __init__(self, calendar, *, summary_after_close_minutes: int = 30):
        self.calendar = calendar
        self.minutes = summary_after_close_minutes
        self.fired: set[tuple[date, str, str | None]] = set()
        self._schedules: dict[date, SessionSchedule] = {}

    def schedule(self, d: date) -> SessionSchedule | None:
        if d not in self._schedules:
            s = self.calendar.session(d)
            if s is None:
                return None
            self._schedules[d] = SessionSchedule.build(s, summary_after_close_minutes=self.minutes)
        return self._schedules[d]

    def due(self, now: datetime, *, catch_up_from: datetime | None = None) -> list[ScheduleEvent]:
        """Events with ts <= now not yet fired. On a fresh start events earlier than ``catch_up_from`` are marked
        fired without being returned (a daemon started at 14:00 must not run the 09:00 pre-open logic)."""
        sch = self.schedule(now.astimezone(NY).date())
        if sch is None:
            return []
        out = []
        for e in sch.events:
            key = (e.session_date, e.kind, e.timeframe)
            if key in self.fired or e.ts > now:
                continue
            self.fired.add(key)
            if catch_up_from is not None and e.ts < catch_up_from:
                continue
            out.append(e)
        return out

    def next_event(self, now: datetime) -> ScheduleEvent | None:
        d = now.astimezone(NY).date()
        for _ in range(10):
            sch = self.schedule(d)
            if sch is not None:
                for e in sch.events:
                    if e.ts > now and (e.session_date, e.kind, e.timeframe) not in self.fired:
                        return e
            d += timedelta(days=1)
        return None
