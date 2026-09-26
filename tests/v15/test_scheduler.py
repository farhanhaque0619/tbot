from datetime import date, datetime, time

from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from bot.runtime.scheduler import Scheduler, SessionSchedule

CAL = SessionCalendar()


def test_schedule_marks_are_relative_to_the_close():
    normal = SessionSchedule.build(CAL.session(date(2026, 9, 22)))
    kinds = {(e.kind, e.timeframe): e.ts.time() for e in normal.events}
    assert kinds[("pre_open", None)] == time(9, 0) and kinds[("pre_open", "protect")] == time(9, 29) and kinds[("session_open", None)] == time(9, 30)
    assert kinds[("t1530", None)] == time(15, 30) and kinds[("t1550", None)] == time(15, 50) and kinds[("t1558", None)] == time(15, 58)
    assert kinds[("session_close", None)] == time(16, 0) and kinds[("post_close", None)] == time(16, 30) and kinds[("evening", None)] == time(19, 5)
    early = SessionSchedule.build(CAL.session(date(2026, 11, 27)))
    k2 = {e.kind: e.ts.time() for e in early.events if e.timeframe is None}
    assert k2["t1530"] == time(12, 30) and k2["t1550"] == time(12, 50) and k2["t1558"] == time(12, 58) and k2["session_close"] == time(13, 0)
    assert [e.ts for e in early.events] == sorted(e.ts for e in early.events)


def test_due_is_idempotent_and_catches_up_silently():
    sch = Scheduler(CAL)
    d = date(2026, 9, 22)
    t = datetime.combine(d, time(15, 51), NY)
    first = sch.due(t, catch_up_from=t)
    assert first == [], "everything before the start time is marked fired, not replayed"
    assert sch.due(datetime.combine(d, time(15, 58), NY)) and sch.due(datetime.combine(d, time(15, 58), NY)) == []
    later = sch.due(datetime.combine(d, time(16, 31), NY))
    assert [e.kind for e in later] == ["session_close", "post_close"]
    nxt = sch.next_event(datetime.combine(d, time(16, 31), NY))
    assert nxt.kind == "evening"
    assert sch.due(datetime.combine(date(2026, 9, 26), time(10, 0), NY)) == [], "Saturday: no session, no events"
    assert sch.next_event(datetime.combine(date(2026, 9, 26), time(10, 0), NY)).session_date == date(2026, 9, 28)
