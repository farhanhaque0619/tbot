"""Health-check semantics shared by `doctor` (and reusable by `live check`).

Levels: OK (everything required works), DEGRADED (works, but a non-required or transient input is off - e.g. the
last daily bar has not been published yet, or a quote is stale while the market is open), FAILED (a required
check errored or a safety-relevant condition is wrong). A doctor run is only "OK" when every check is OK.
"""
from __future__ import annotations

from dataclasses import dataclass, field

OK, DEGRADED, FAILED = "OK", "DEGRADED", "FAILED"
_RANK = {OK: 0, DEGRADED: 1, FAILED: 2}


@dataclass
class HealthCheck:
    name: str
    level: str
    detail: str = ""
    required: bool = True


@dataclass
class HealthReport:
    checks: list[HealthCheck] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str = "", *, required: bool = True) -> None:
        if level not in _RANK:
            raise ValueError(level)
        self.checks.append(HealthCheck(name, level, detail, required))

    @property
    def level(self) -> str:
        worst = OK
        for c in self.checks:
            lvl = c.level
            # a FAILED optional check degrades but does not fail the whole report
            if lvl == FAILED and not c.required:
                lvl = DEGRADED
            if _RANK[lvl] > _RANK[worst]:
                worst = lvl
        return worst

    @property
    def exit_code(self) -> int:
        return {OK: 0, DEGRADED: 2, FAILED: 1}[self.level]

    def problems(self) -> list[HealthCheck]:
        return [c for c in self.checks if c.level != OK]


def classify_quote_age(age_seconds: float | None, *, market_open: bool, max_stale_seconds: int) -> tuple[str, str]:
    """Quote freshness matters only while the market is open; outside hours an old quote is expected."""
    if age_seconds is None:
        return DEGRADED, "quote unavailable"
    if market_open and age_seconds > max_stale_seconds:
        return DEGRADED, f"quote {age_seconds:.0f}s old while market open (limit {max_stale_seconds}s)"
    if not market_open:
        return OK, f"quote {age_seconds:.0f}s old (market closed; age informational)"
    return OK, f"quote {age_seconds:.0f}s old"


def classify_bar_currency(last_bar_date, last_session_date) -> tuple[str, str]:
    """The last completed session's daily bar should be cached. Missing it is DEGRADED (it may not be published
    yet on the free plan / right after the close); an error fetching bars at all is FAILED (handled by caller)."""
    if last_bar_date is None:
        return FAILED, f"no daily bars available (last completed session {last_session_date})"
    if last_bar_date == last_session_date:
        return OK, f"last bar {last_bar_date} == last completed session"
    if last_bar_date > last_session_date:
        return DEGRADED, f"last bar {last_bar_date} is AFTER the last completed session {last_session_date} (calendar/clock disagreement?)"
    return DEGRADED, f"last bar {last_bar_date} lags last completed session {last_session_date} (not published yet, or data gap)"
