"""Minute bars: aggregation to N-minute bars on exact ET session boundaries, and resumable history fetch (Phase 1).

Bar timestamps are bar STARTS (Alpaca convention). An N-minute bar starting at 09:30 covers [09:30, 09:30+N).
The last bar of a session is flagged ``is_session_end``; if the session closes before the bar's natural end
(early close at 13:00 with 60-minute bars, say) it is also ``partial``. Strategies must treat a partial bar as
complete only when ``is_session_end`` is true.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from bot.data.store import BarStore

log = logging.getLogger(__name__)

AGG_COLUMNS = ["open", "high", "low", "close", "volume", "trade_count", "vwap", "n_minutes", "partial", "is_session_end", "session_date"]


def aggregate(bars_1m: pd.DataFrame, minutes: int, calendar: SessionCalendar) -> pd.DataFrame:
    """Aggregate 1-minute bars (tz-aware NY index, bar starts) to ``minutes``-minute bars aligned to each session's
    open. Bars outside regular hours are dropped. Returns a frame indexed by bar start (NY)."""
    if minutes < 1:
        raise ValueError("minutes must be >= 1")
    if bars_1m.empty:
        return pd.DataFrame(columns=AGG_COLUMNS, index=pd.DatetimeIndex([], tz="America/New_York", name="ts"))
    idx = pd.DatetimeIndex(bars_1m.index)
    if idx.tz is None:
        raise ValueError("minute bars must be tz-aware")
    df = bars_1m.copy()
    df.index = idx.tz_convert(NY)
    rows = []
    for d, day in df.groupby(df.index.date):
        s = calendar.session(d)
        if s is None:
            log.debug("dropping %d bars on non-session date %s", len(day), d)
            continue
        day = day[(day.index >= s.open) & (day.index < s.close)]
        if day.empty:
            continue
        offset_min = ((day.index - s.open).total_seconds() // 60).astype(int)
        bucket = offset_min // minutes
        for k, g in day.groupby(bucket):
            start = s.open + timedelta(minutes=int(k) * minutes)
            natural_end = start + timedelta(minutes=minutes)
            end = min(natural_end, s.close)
            vol = float(g["volume"].sum())
            vwap = float((g["vwap"].fillna(g["close"]) * g["volume"]).sum() / vol) if vol > 0 and "vwap" in g else float("nan")
            rows.append({"ts": start, "open": float(g["open"].iloc[0]), "high": float(g["high"].max()), "low": float(g["low"].min()),
                         "close": float(g["close"].iloc[-1]), "volume": vol,
                         "trade_count": float(g["trade_count"].sum()) if "trade_count" in g else float("nan"), "vwap": vwap,
                         "n_minutes": int(len(g)), "partial": bool(natural_end > s.close), "is_session_end": bool(end == s.close),
                         "session_date": d})
    if not rows:
        return pd.DataFrame(columns=AGG_COLUMNS, index=pd.DatetimeIndex([], tz="America/New_York", name="ts"))
    out = pd.DataFrame(rows).set_index("ts").sort_index()
    out.index.name = "ts"
    return out[AGG_COLUMNS]


@dataclass
class FetchSummary:
    symbol: str
    feed: str
    sessions_requested: int
    sessions_fetched: int
    bars_written: int
    incomplete: list[date]


def fetch_minute_history(store: BarStore, provider, symbol: str, start: date, end: date, *, feed: str,
                         calendar: SessionCalendar, chunk_sessions: int = 5, now: datetime | None = None,
                         force: bool = False) -> FetchSummary:
    """Fetch and cache 1-minute bars session by session (in chunks). Resumable: sessions already marked complete
    in ``minute_coverage`` are skipped unless ``force``. A session is complete when it holds every expected minute
    or, for the current session, when its close is at least 16 minutes in the past for SIP."""
    now = (now or datetime.now(NY)).astimezone(NY)
    sessions = calendar.sessions_between(start, end)
    covered = store.minute_coverage(symbol, feed)
    todo = [s for s in sessions if force or not covered.get(s.date, (0, False))[1]]
    written, fetched, incomplete = 0, 0, []
    for i in range(0, len(todo), chunk_sessions):
        chunk = todo[i:i + chunk_sessions]
        c_start, c_end = chunk[0].open, chunk[-1].close
        df = provider.fetch_minute(symbol, c_start, c_end, feed=feed, now=now)
        if not df.empty:
            written += store.upsert_minute_bars(symbol, df, feed=feed)
        for s in chunk:
            got = df[(df.index >= s.open) & (df.index < s.close)] if not df.empty else df
            expected = int((s.close - s.open).total_seconds() // 60)
            # the SIP lag hides the last 16 minutes of a session that closed less than 16 minutes ago
            final = (now - s.close) >= timedelta(minutes=16) if feed == "sip" else now >= s.close
            complete = final and len(got) >= expected * 0.98      # tolerate a few missing minutes on thin names
            if final and not complete:
                incomplete.append(s.date)
            store.set_minute_coverage(symbol, feed, s.date, len(got), complete)
            fetched += 1
    return FetchSummary(symbol.upper(), feed, len(sessions), fetched, written, incomplete)
