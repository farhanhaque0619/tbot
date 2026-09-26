"""Data-quality checks for cached daily bars (Phase 2).

Reports: coverage, source, missing weekdays (holidays are not distinguishable offline, so they are listed and
counted rather than flagged as errors), duplicate timestamps, non-positive prices, OHLC consistency, suspicious
one-day moves (likely unadjusted corporate actions), close-only (synthetic OHLC) data, and timezone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from bot.data.store import BarStore

JUMP_THRESHOLD = 0.40   # |1-day return| above this is flagged as a possible split / bad print


@dataclass
class QualityReport:
    symbol: str
    bars: int = 0
    first: date | None = None
    last: date | None = None
    source: str = ""
    tz: str = ""
    missing_weekdays: list[date] = field(default_factory=list)
    duplicates: int = 0
    nonpositive: int = 0
    ohlc_inconsistent: int = 0
    suspicious_jumps: list[tuple[date, float]] = field(default_factory=list)
    synthetic_ohlc: bool = False
    zero_volume_share: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.bars > 0 and self.duplicates == 0 and self.nonpositive == 0 and self.ohlc_inconsistent == 0

    def render(self) -> str:
        lines = [f"[{self.symbol}] {self.bars} bars {self.first} → {self.last}  source={self.source or '-'}  tz={self.tz}",
                 f"  missing weekdays: {len(self.missing_weekdays)} (holidays included; e.g. {[str(d) for d in self.missing_weekdays[:5]]})",
                 f"  duplicates={self.duplicates} nonpositive={self.nonpositive} ohlc_inconsistent={self.ohlc_inconsistent} "
                 f"zero-volume share={self.zero_volume_share:.0%} synthetic_ohlc={self.synthetic_ohlc}",
                 f"  suspicious 1-day moves (> {JUMP_THRESHOLD:.0%}): {[(str(d), round(r, 3)) for d, r in self.suspicious_jumps[:8]]}"]
        lines += [f"  note: {n}" for n in self.notes]
        lines.append("  status: " + ("OK" if self.ok else "PROBLEMS"))
        return "\n".join(lines)


def check_frame(symbol: str, df: pd.DataFrame, source: str = "") -> QualityReport:
    rep = QualityReport(symbol=symbol.upper(), source=source)
    if df.empty:
        rep.notes.append("no bars")
        return rep
    idx = pd.DatetimeIndex(df.index)
    rep.bars, rep.first, rep.last, rep.tz = len(df), idx[0].date(), idx[-1].date(), str(idx.tz)
    rep.duplicates = int(idx.duplicated().sum())
    expected = pd.bdate_range(idx[0].normalize(), idx[-1].normalize(), tz=idx.tz)
    have = set(idx.normalize())
    rep.missing_weekdays = [d.date() for d in expected if d not in have]
    px = df[["open", "high", "low", "close"]].astype(float)
    rep.nonpositive = int((px <= 0).any(axis=1).sum())
    rep.ohlc_inconsistent = int(((df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))).sum())
    r = df["close"].astype(float).pct_change()
    jumps = r[r.abs() > JUMP_THRESHOLD]
    rep.suspicious_jumps = [(t.date(), float(v)) for t, v in jumps.items()]
    rep.synthetic_ohlc = bool(len(df) > 5 and np.allclose(df["open"], df["close"]) and np.allclose(df["high"], df["close"]))
    if "volume" in df:
        rep.zero_volume_share = float((df["volume"].fillna(0) <= 0).mean())
    if rep.synthetic_ohlc:
        rep.notes.append("close-only data: open/high/low equal close; fills happen at next close, ATR uses close-to-close only")
    if rep.suspicious_jumps:
        rep.notes.append("large one-day moves: verify against a split calendar (unadjusted split?) or a bad print")
    if len(rep.missing_weekdays) > 0.06 * len(expected):
        rep.notes.append("more than ~6% of weekdays missing: beyond normal holidays, check the source")
    return rep


def check_symbol(store: BarStore, symbol: str, *, adjustment: str = "split") -> QualityReport:
    df = store.get_bars(symbol, adjustment=adjustment)
    summ = store.summary()
    row = summ[(summ["symbol"] == symbol.upper()) & (summ["adjustment"] == adjustment)]
    src = str(row["source"].iloc[0]) if len(row) else ""
    return check_frame(symbol, df, src)


# ------------------------------------------------------------------ minute data (Phase 1)
@dataclass
class MinuteQualityReport:
    symbol: str
    feed: str = ""
    sessions: int = 0
    first: date | None = None
    last: date | None = None
    missing_minutes: dict[date, int] = field(default_factory=dict)
    incomplete_sessions: list[date] = field(default_factory=list)
    duplicates: int = 0
    ohlc_inconsistent: int = 0
    outside_session: int = 0
    zero_volume_runs: dict[date, int] = field(default_factory=dict)   # longest run per session (>= 5 only)
    split_discontinuities: list[tuple[date, float]] = field(default_factory=list)
    backfilled_share: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.sessions > 0 and self.duplicates == 0 and self.ohlc_inconsistent == 0 and self.outside_session == 0 and not self.incomplete_sessions

    def render(self) -> str:
        lines = [f"[{self.symbol} 1m {self.feed}] {self.sessions} sessions {self.first} → {self.last}",
                 f"  incomplete sessions (>2% minutes missing): {len(self.incomplete_sessions)} e.g. {[str(d) for d in self.incomplete_sessions[:5]]}",
                 f"  total missing minutes: {sum(self.missing_minutes.values())} · duplicates={self.duplicates} ohlc_inconsistent={self.ohlc_inconsistent} "
                 f"outside_session={self.outside_session} · backfilled share={self.backfilled_share:.1%}",
                 f"  zero-volume runs >= 5 min: {len(self.zero_volume_runs)} sessions · split discontinuities: {[(str(d), round(r, 3)) for d, r in self.split_discontinuities[:5]]}"]
        lines += [f"  note: {n}" for n in self.notes]
        lines.append("  status: " + ("OK" if self.ok else "PROBLEMS"))
        return "\n".join(lines)


def check_minute_frame(symbol: str, df: pd.DataFrame, calendar, *, feed: str = "", missing_tolerance: float = 0.02) -> MinuteQualityReport:
    rep = MinuteQualityReport(symbol=symbol.upper(), feed=feed)
    if df.empty:
        rep.notes.append("no minute bars")
        return rep
    idx = pd.DatetimeIndex(df.index)
    rep.duplicates = int(idx.duplicated().sum())
    df = df[~idx.duplicated()]
    idx = pd.DatetimeIndex(df.index)
    px = df[["open", "high", "low", "close"]].astype(float)
    rep.ohlc_inconsistent = int(((df["high"] < px[["open", "close"]].max(axis=1)) | (df["low"] > px[["open", "close"]].min(axis=1)) | (px <= 0).any(axis=1)).sum())
    if "backfilled" in df:
        rep.backfilled_share = float(df["backfilled"].astype(bool).mean())
    prev_close = None
    dates = sorted(set(idx.date))
    rep.sessions, rep.first, rep.last = len(dates), dates[0], dates[-1]
    for d in dates:
        day = df[idx.date == d]
        s = calendar.session(d)
        if s is None:
            rep.outside_session += len(day)
            continue
        inside = day[(day.index >= s.open) & (day.index < s.close)]
        rep.outside_session += len(day) - len(inside)
        expected = int((s.close - s.open).total_seconds() // 60)
        missing = expected - len(inside)
        if missing > 0:
            rep.missing_minutes[d] = missing
        if missing > expected * missing_tolerance:
            rep.incomplete_sessions.append(d)
        if "volume" in inside and len(inside):
            z = (inside["volume"].fillna(0) <= 0).to_numpy()
            run = best = 0
            for v in z:
                run = run + 1 if v else 0
                best = max(best, run)
            if best >= 5:
                rep.zero_volume_runs[d] = best
        if prev_close is not None and len(inside):
            jump = float(inside["open"].iloc[0] / prev_close - 1)
            if abs(jump) > 0.20:
                rep.split_discontinuities.append((d, jump))
        if len(inside):
            prev_close = float(inside["close"].iloc[-1])
    if rep.split_discontinuities:
        rep.notes.append("session-to-session open/close jump > 20%: unadjusted split or bad print")
    if rep.outside_session:
        rep.notes.append("bars outside regular hours or on non-session dates are present (extended-hours data?)")
    return rep
