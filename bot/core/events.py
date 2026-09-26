"""Typed events on the internal bus (Phase 2). Frozen dataclasses; timestamps are tz-aware America/New_York."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

SCHEDULE_KINDS = ("pre_open", "session_open", "bar_close", "t1530", "t1550", "t1558", "session_close", "post_close", "evening")


@dataclass(frozen=True)
class BarEvent:
    symbol: str
    ts: datetime                 # bar START
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str               # "1m" | "30m" | "1d"
    session_date: date
    is_session_end: bool = False
    partial: bool = False
    vwap: float | None = None
    trade_count: float | None = None
    backfilled: bool = False

    @property
    def end(self) -> datetime:
        from datetime import timedelta
        n = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1d": 390}.get(self.timeframe, 1)
        return self.ts + timedelta(minutes=n)


@dataclass(frozen=True)
class QuoteEvent:
    symbol: str
    ts: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else (self.ask or self.bid)

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m * 1e4 if m > 0 and self.bid > 0 and self.ask > 0 else float("inf")


@dataclass(frozen=True)
class TradeUpdateEvent:
    order_id: str
    client_order_id: str
    event: str                   # new | fill | partial_fill | canceled | rejected | expired | replaced | done_for_day
    ts: datetime
    symbol: str
    side: str
    qty: float
    filled_qty: float
    price: float | None
    status: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        """Idempotency key: duplicates from a reconnect collapse on (order_id, event, timestamp)."""
        return (self.order_id, self.event, self.ts.isoformat())


@dataclass(frozen=True)
class ScheduleEvent:
    kind: str
    ts: datetime
    session_date: date
    timeframe: str | None = None      # for bar_close: "30m"

    def __post_init__(self):
        if self.kind not in SCHEDULE_KINDS:
            raise ValueError(f"unknown schedule kind {self.kind!r}")
