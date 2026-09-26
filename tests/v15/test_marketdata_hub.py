"""MarketDataHub offline: dedup, out-of-order, staleness, 406, reconnect backfill, 30m aggregation, subscriptions."""
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from bot.core.bus import EventBus
from bot.core.events import BarEvent, QuoteEvent
from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from bot.stream.marketdata import MarketDataHub

CAL = SessionCalendar()
D = date(2026, 9, 22)


def ts(hh, mm):
    return datetime.combine(D, time(hh, mm), NY)


def raw(sym, t, c=100.0):
    return {"T": "b", "S": sym, "t": t, "o": c - 0.1, "h": c + 0.2, "l": c - 0.2, "c": c, "v": 1000, "vw": c, "n": 10}


class FakeStream:
    def __init__(self, hub):
        self.hub, self.subs, self.quotes, self.stopped, self.ran = hub, [], set(), False, False

    def subscribe_bars(self, h, *syms): self.subs.extend(syms)
    def subscribe_quotes(self, h, *syms): self.quotes |= set(syms)
    def unsubscribe_quotes(self, *syms): self.quotes -= set(syms)
    def run(self): self.ran = True
    def stop(self): self.stopped = True


class StubProvider:
    def __init__(self):
        self.calls = []

    def fetch_minute(self, symbol, start, end, *, feed=None, now=None):
        self.calls.append((symbol, start, end, feed))
        idx = pd.date_range(start, end - timedelta(minutes=1), freq="1min")
        return pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 500.0, "vwap": 100.1}, index=idx)


def make(**kw):
    bus = EventBus()
    alerts = []
    clock = kw.pop("clock", lambda: ts(10, 5))
    hub = MarketDataHub(symbols=["SPY", "QQQ"], calendar=CAL, bus=bus, stream_factory=FakeStream, clock=clock,
                        alert=lambda t, m, level="info": alerts.append((t, level)), **kw)
    return hub, bus, alerts


def test_dedup_and_out_of_order_and_publish():
    hub, bus, _ = make()
    assert hub.on_bar_raw(raw("SPY", ts(10, 0))) is not None
    assert hub.on_bar_raw(raw("SPY", ts(10, 0))) is None and hub.stats["duplicates"] == 1
    assert hub.on_bar_raw(raw("SPY", ts(9, 59))) is None and hub.stats["out_of_order"] == 1
    assert hub.on_bar_raw(raw("SPY", "2026-09-22T14:01:00Z")) is not None, "UTC string timestamps are normalised to New York"
    evs = bus.drain()
    assert [e.ts for e in evs] == [ts(10, 0), ts(10, 1)] and all(isinstance(e, BarEvent) and e.timeframe == "1m" for e in evs)
    assert evs[0].vwap == 100.0 and evs[0].trade_count == 10 and not evs[0].backfilled


def test_last_bar_age_and_quotes():
    hub, bus, _ = make(clock=lambda: ts(10, 5))
    assert hub.last_bar_age("SPY") is None
    hub.on_bar_raw(raw("SPY", ts(10, 0)))
    assert hub.last_bar_age("SPY") == pytest.approx(4 * 60)      # bar 10:00 ends 10:01; now 10:05
    q = hub.on_quote_raw({"T": "q", "S": "SPY", "t": ts(10, 4), "bp": 99.99, "ap": 100.01, "bs": 5, "as": 7})
    assert isinstance(q, QuoteEvent) and q.spread_bps == pytest.approx(2.0, rel=1e-3) and hub.stats["quotes"] == 1


def test_thirty_minute_aggregation_with_partial_last_bucket():
    hub, bus, _ = make()
    sess = CAL.session(D)
    t = sess.open
    n = 0
    while t < sess.close:
        hub.on_bar_raw(raw("SPY", t, c=100 + n * 0.01))
        t += timedelta(minutes=1); n += 1
    bars30 = [e for e in bus.drain() if isinstance(e, BarEvent) and e.timeframe == "30m"]
    assert len(bars30) == 13 and bars30[0].ts == sess.open and bars30[-1].partial is False and bars30[-1].is_session_end
    assert bars30[0].close == pytest.approx(100 + 29 * 0.01) and bars30[0].volume == 30 * 1000
    # early close day: last bucket partial? 13:00 close = 210 minutes = 7 full buckets -> not partial; a truncated feed is
    hub2, bus2, _ = make()
    e = CAL.session(date(2026, 11, 27))
    t = e.open
    while t < e.close - timedelta(minutes=10):
        hub2.on_bar_raw(raw("SPY", t)); t += timedelta(minutes=1)
    hub2.on_bar_raw(raw("SPY", e.close - timedelta(minutes=1)))       # last minute flagged session end
    b30 = [x for x in bus2.drain() if isinstance(x, BarEvent) and x.timeframe == "30m"]
    assert b30[-1].partial is True and b30[-1].is_session_end


def test_406_is_fatal_and_second_start_refused():
    hub, bus, alerts = make()
    hub.start()
    assert hub._stream.subs == ["SPY", "QQQ"] and hub._stream.ran
    with pytest.raises(RuntimeError, match="second connection"):
        hub.start()
    hub.on_error(406, "connection limit exceeded")
    assert hub.fatal and hub._stream.stopped and ("market data: connection limit (406)", "critical") in alerts
    hub.on_error(400, "invalid syntax")
    assert hub.stats["errors"] == 2
    with pytest.raises(ValueError, match="cap"):
        MarketDataHub(symbols=[f"S{i}" for i in range(31)], calendar=CAL, bus=bus)


def test_reconnect_backfills_gap_with_sip_then_iex_and_marks_bars():
    prov = StubProvider()
    hub, bus, alerts = make(provider=prov, clock=lambda: ts(10, 30))
    hub.on_connected()
    hub.on_bar_raw(raw("SPY", ts(10, 0)))
    hub.on_disconnected()
    assert not hub.connected
    hub.on_connected()
    assert hub.connected and hub.stats["reconnects"] == 1 and ("market data reconnected", "warning") in alerts
    assert prov.calls[0][:2] == ("SPY", ts(10, 1)) and prov.calls[0][3] == "sip" and prov.calls[0][2] == ts(10, 14)
    assert prov.calls[1][1] == ts(10, 14) and prov.calls[1][3] == "iex" and prov.calls[1][2] == ts(10, 30)
    evs = [e for e in bus.drain() if isinstance(e, BarEvent) and e.timeframe == "1m"]
    assert evs[0].ts == ts(10, 0) and not evs[0].backfilled and all(e.backfilled for e in evs[1:]) and evs[-1].ts == ts(10, 29)
    assert hub.stats["backfilled"] == 29 and hub.last_bar["SPY"] == ts(10, 29)
    # a live bar older than the backfilled ones is dropped as out of order
    assert hub.on_bar_raw(raw("SPY", ts(10, 20))) is None


def test_quote_subscriptions_follow_positions():
    hub, bus, _ = make()
    hub.start(); hub.on_connected()
    add, rem = hub.set_quote_symbols({"SPY", "XYZ"})
    assert add == {"SPY"} and rem == set() and hub._stream.quotes == {"SPY"}, "unknown symbols are ignored"
    add, rem = hub.set_quote_symbols({"QQQ"})
    assert add == {"QQQ"} and rem == {"SPY"} and hub._stream.quotes == {"QQQ"}
