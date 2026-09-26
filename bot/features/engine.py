"""FeatureEngine (Phase 2, spec §5.2): incremental, O(1) per bar per feature, per symbol.

Consumes BarEvents (1m, 30m, 1d) and QuoteEvents; ``snapshot(symbol)`` returns a FeatureSnapshot whose fields are
None until enough history exists. Daily bars are either fed explicitly (daily-legacy mode) or synthesised from the
session's 1-minute bars when the session-end bar arrives (minute mode). All arithmetic, no models.
"""
from __future__ import annotations

import csv
import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from statistics import median

from bot.core.events import BarEvent, QuoteEvent
from bot.strategies.indicators import RollingATR

ANN_1M = math.sqrt(390 * 252)
ANN_30M = math.sqrt(13 * 252)
ANN_D = math.sqrt(252)


def _std(xs, ddof: int = 1) -> float | None:
    n = len(xs)
    if n <= ddof:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def load_earnings(path: str | Path = "config/earnings.csv") -> dict[str, set[date]]:
    p = Path(path)
    out: dict[str, set[date]] = {}
    if not p.exists():
        return out
    with p.open() as f:
        for row in csv.DictReader(f):
            try:
                out.setdefault(row["symbol"].upper(), set()).add(date.fromisoformat(row["date"].strip()))
            except (KeyError, ValueError):
                continue
    return out


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    asof: datetime
    last_close: float | None = None
    prev_session_close: float | None = None
    session_open: float | None = None
    vol_1m_21: float | None = None
    vol_1m_63: float | None = None
    vol_30m_21: float | None = None
    vol_30m_63: float | None = None
    vol_d_21: float | None = None
    vol_d_63: float | None = None
    r1: float | None = None                 # ln(close of 09:30-10:00 bar / prev close)
    r12: float | None = None                # ln(close 15:00-15:30 / close 14:30-15:00)
    sigma_last30: float | None = None       # 21-session std of the 15:30-16:00 return
    session_vwap: float | None = None
    beta_60d: float | None = None
    res5: float | None = None
    sigma_res_21d: float | None = None
    spread_bps: float | None = None
    spread_age_s: float | None = None
    relvol_1515: float | None = None
    ret_12m_ex_1m: float | None = None
    earnings_within_5: bool = False
    synthetic_frac_30m: float | None = None
    atr14_d: float | None = None
    session_volume: float = 0.0
    daily_bars: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["asof"] = self.asof.isoformat()
        return d


class _SymbolState:
    def __init__(self):
        self.daily_close: deque[float] = deque(maxlen=300)
        self.daily_ret: deque[tuple[date, float]] = deque(maxlen=300)
        self.atr = RollingATR(14)
        self.ret1m: deque[float] = deque(maxlen=64)
        self.ret30: deque[float] = deque(maxlen=64)
        self.last1m_close: float | None = None
        self.last30_close: float | None = None
        self.prev_close: float | None = None
        self.session: date | None = None
        self.session_open: float | None = None
        self.session_high = -math.inf
        self.session_low = math.inf
        self.session_close: float | None = None
        self.session_pv = 0.0
        self.session_v = 0.0
        self.vol_to_1515 = 0.0
        self.first30_close: float | None = None
        self.c1500: float | None = None
        self.c1530: float | None = None
        self.last30_hist: deque[float] = deque(maxlen=21)
        self.vol1515_hist: deque[float] = deque(maxlen=21)
        self.recent_synth: deque[bool] = deque(maxlen=30)
        self.quote_spread: float | None = None
        self.quote_ts: datetime | None = None


class FeatureEngine:
    def __init__(self, *, benchmark: str = "SPY", earnings: dict[str, set[date]] | None = None, volume_source: str = "sip", calendar=None):
        self.benchmark = benchmark.upper()
        self.earnings = earnings or {}
        self.volume_source = volume_source
        self.calendar = calendar
        self.s: dict[str, _SymbolState] = {}

    def _st(self, symbol: str) -> _SymbolState:
        return self.s.setdefault(symbol.upper(), _SymbolState())

    # ------------------------------------------------------------------ input
    def on_quote(self, ev: QuoteEvent) -> None:
        st = self._st(ev.symbol)
        st.quote_spread, st.quote_ts = ev.spread_bps, ev.ts

    def on_bar(self, ev: BarEvent) -> None:
        st = self._st(ev.symbol)
        if ev.timeframe == "1d":
            self._on_daily(st, ev.session_date, ev.open, ev.high, ev.low, ev.close, ev.volume)
            return
        if st.session != ev.session_date:
            self._start_session(st, ev.session_date, ev.open)
        if ev.timeframe == "1m":
            if st.last1m_close:
                st.ret1m.append(math.log(ev.close / st.last1m_close))
            st.last1m_close = ev.close
            st.session_high, st.session_low = max(st.session_high, ev.high), min(st.session_low, ev.low)
            st.session_close = ev.close
            st.session_pv += (ev.vwap if ev.vwap else ev.close) * ev.volume
            st.session_v += ev.volume
            if ev.ts.timetz().replace(tzinfo=None) < time(15, 15):
                st.vol_to_1515 += ev.volume
            st.recent_synth.append(bool(ev.backfilled))
            if ev.is_session_end:
                self._on_daily(st, ev.session_date, st.session_open, st.session_high, st.session_low, ev.close, st.session_v, from_minutes=True)
        elif ev.timeframe == "30m":
            if st.last30_close:
                st.ret30.append(math.log(ev.close / st.last30_close))
            st.last30_close = ev.close
            t = ev.ts.timetz().replace(tzinfo=None)
            if t == time(9, 30):
                st.first30_close = ev.close
            elif t == time(14, 30):
                st.c1500 = ev.close
            elif t == time(15, 0):
                st.c1530 = ev.close
            elif t == time(15, 30) and st.c1530:
                st.last30_hist.append(math.log(ev.close / st.c1530))

    def _start_session(self, st: _SymbolState, d: date, open_: float) -> None:
        if st.session is not None and st.session_close is not None:
            st.prev_close = st.session_close
        st.session, st.session_open = d, open_
        st.session_high, st.session_low, st.session_close = -math.inf, math.inf, None
        st.session_pv = st.session_v = st.vol_to_1515 = 0.0
        st.first30_close = st.c1500 = st.c1530 = None
        st.last1m_close = None
        st.last30_close = None

    def _on_daily(self, st: _SymbolState, d: date, o: float, h: float, l_: float, c: float, v: float, *, from_minutes: bool = False) -> None:
        if st.daily_close:
            st.daily_ret.append((d, math.log(c / st.daily_close[-1])))
        st.daily_close.append(c)
        st.atr.update(h, l_, c)
        if st.vol_to_1515 > 0:
            st.vol1515_hist.append(st.vol_to_1515)
        if not from_minutes:
            st.prev_close, st.session_close, st.session = st.session_close if st.session_close is not None else st.prev_close, c, d
            if st.session_open is None:
                st.session_open = o

    # ---------------------------------------------------------------- output
    def snapshot(self, symbol: str, asof: datetime) -> FeatureSnapshot:
        st = self._st(symbol)
        b = self.s.get(self.benchmark)
        closes = list(st.daily_close)
        drets = [r for _, r in st.daily_ret]
        prev_close = st.prev_close if st.prev_close is not None else (closes[-2] if len(closes) > 1 else None)
        # between sessions (pre_open / session_open before the first bar) the last completed session is the previous one
        pre_session = st.session is not None and asof.date() > st.session
        if pre_session:
            prev_close = st.session_close if st.session_close is not None else (closes[-1] if closes else None)
        r1 = math.log(st.first30_close / prev_close) if (st.first30_close and prev_close and not pre_session) else None
        r12 = math.log(st.c1530 / st.c1500) if (st.c1530 and st.c1500 and not pre_session) else None
        beta = res5 = sigma_res = None
        if b is not None and len(st.daily_ret) >= 60 and len(b.daily_ret) >= 60:
            bmap = dict(b.daily_ret)
            pairs = [(r, bmap[d]) for d, r in list(st.daily_ret)[-60:] if d in bmap]
            if len(pairs) >= 40:
                xs, ys = [p[1] for p in pairs], [p[0] for p in pairs]
                mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                var = sum((x - mx) ** 2 for x in xs)
                if var > 0:
                    beta = sum((x - mx) * (y - my) for x, y in pairs) / var
                    resid = [y - beta * x for x, y in pairs]
                    sigma_res = _std(resid[-21:])
                    res5 = sum(y for _, y in pairs[-5:]) - beta * sum(x for x, _ in pairs[-5:])
        spread_age = (asof - st.quote_ts).total_seconds() if st.quote_ts else None
        relvol = None
        if self.volume_source == "sip" and st.vol_to_1515 > 0 and len(st.vol1515_hist) >= 10:
            med = median(st.vol1515_hist)
            relvol = st.vol_to_1515 / med if med > 0 else None
        ret12 = (closes[-22] / closes[-253] - 1) if len(closes) >= 253 else None
        earn = False
        if st.session and self.earnings.get(symbol.upper()):
            earn = any(0 <= (e - st.session).days <= 7 for e in self.earnings[symbol.upper()])
        return FeatureSnapshot(
            symbol=symbol.upper(), asof=asof, last_close=st.session_close or (closes[-1] if closes else None), prev_session_close=prev_close,
            session_open=None if pre_session else st.session_open,
            vol_1m_21=(_std(list(st.ret1m)[-21:]) or 0) * ANN_1M if len(st.ret1m) >= 21 else None,
            vol_1m_63=(_std(list(st.ret1m)[-63:]) or 0) * ANN_1M if len(st.ret1m) >= 63 else None,
            vol_30m_21=(_std(list(st.ret30)[-21:]) or 0) * ANN_30M if len(st.ret30) >= 21 else None,
            vol_30m_63=(_std(list(st.ret30)[-63:]) or 0) * ANN_30M if len(st.ret30) >= 63 else None,
            vol_d_21=(_std(drets[-21:]) or 0) * ANN_D if len(drets) >= 21 else None,
            vol_d_63=(_std(drets[-63:]) or 0) * ANN_D if len(drets) >= 63 else None,
            r1=r1, r12=r12, sigma_last30=_std(list(st.last30_hist)) if len(st.last30_hist) >= 10 else None,
            session_vwap=(st.session_pv / st.session_v) if (st.session_v > 0 and not pre_session) else None,
            beta_60d=beta, res5=res5, sigma_res_21d=sigma_res, spread_bps=st.quote_spread, spread_age_s=spread_age, relvol_1515=relvol,
            ret_12m_ex_1m=ret12, earnings_within_5=earn,
            synthetic_frac_30m=(sum(st.recent_synth) / len(st.recent_synth)) if st.recent_synth else None,
            atr14_d=st.atr.atr, session_volume=0.0 if pre_session else st.session_v, daily_bars=len(closes))
