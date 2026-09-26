"""MarketDataHub (spec §9): ONE websocket connection to the Alpaca stock stream, bars for every symbol and quotes for
symbols with a position or open order, deduplicated and ordered, published as BarEvent/QuoteEvent on the internal bus,
with 30-minute aggregation and REST backfill of the gap after a reconnect.

alpaca-py's ``StockDataStream`` owns the socket, authentication, msgpack decoding and its own reconnect with backoff;
the hub wraps it through ``_HubStream`` to see connection changes and error frames (406 = connection limit reached:
fatal, the hub refuses to keep trying and alerts). The hub never holds credentials: the stream is created by a factory
closure supplied by the daemon. Everything except the socket is exercised offline by tests through ``on_bar_raw`` /
``on_quote_raw`` / ``on_error`` / ``on_connected`` / ``on_disconnected``.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from bot.core.events import BarEvent, QuoteEvent
from bot.data.calendar import NY

log = logging.getLogger(__name__)
SIP_LAG = timedelta(minutes=16)
CODE_CONNECTION_LIMIT = 406


def _parse_ts(t: Any) -> datetime:
    if isinstance(t, datetime):
        ts = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    elif isinstance(t, (int, float)):
        ts = datetime.fromtimestamp(t / 1e9 if t > 1e12 else t, tz=timezone.utc)
    else:
        s = str(t).replace("Z", "+00:00")
        ts = datetime.fromisoformat(s)
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(NY)


def make_stream_factory(settings, env: str, feed: str) -> Callable[["MarketDataHub"], Any]:
    """Closure holding the credentials; the hub only ever sees the constructed stream."""
    def factory(hub: "MarketDataHub"):
        from alpaca.data.enums import DataFeed
        key, secret = settings.credentials(env)
        return _HubStream(hub, api_key=key, secret_key=secret, raw_data=True, feed=DataFeed(feed))
    return factory


try:
    from alpaca.data.live import StockDataStream as _Base
except Exception:  # pragma: no cover - alpaca-py always present in this repo
    _Base = object


class _HubStream(_Base):  # type: ignore[misc]
    """StockDataStream with hooks: error frames -> hub.on_error, connect/close -> hub.on_connected/on_disconnected."""

    def __init__(self, hub: "MarketDataHub", **kw):
        super().__init__(**kw)
        self._hub = hub

    async def _dispatch(self, msg):
        if isinstance(msg, dict) and msg.get("T") == "error":
            self._hub.on_error(msg.get("code"), msg.get("msg"))
        await super()._dispatch(msg)

    async def _start_ws(self):
        await super()._start_ws()
        self._hub.on_connected()

    async def close(self):
        await super().close()
        self._hub.on_disconnected()


class Aggregator30m:
    """1-minute bars -> 30-minute bars on session-relative buckets; the last bucket of a session may be partial."""

    def __init__(self, calendar):
        self.calendar = calendar
        self.acc: dict[str, list[BarEvent]] = {}

    def add(self, ev: BarEvent) -> BarEvent | None:
        sess = self.calendar.session(ev.session_date)
        if sess is None:
            return None
        acc = self.acc.setdefault(ev.symbol, [])
        if acc and acc[0].session_date != ev.session_date:
            acc.clear()
        acc.append(ev)
        end_of_bucket = ((ev.ts - sess.open).total_seconds() // 60 + 1) % 30 == 0 or ev.is_session_end
        if not end_of_bucket:
            return None
        b0 = acc[0]
        out = BarEvent(ev.symbol, b0.ts, b0.open, max(x.high for x in acc), min(x.low for x in acc), acc[-1].close, sum(x.volume for x in acc), "30m",
                       ev.session_date, is_session_end=ev.is_session_end, partial=len(acc) < 30 and ev.is_session_end,
                       backfilled=any(x.backfilled for x in acc))
        acc.clear()
        return out


class MarketDataHub:
    def __init__(self, *, symbols: list[str], calendar, bus, feed: str = "iex", stream_factory: Callable[["MarketDataHub"], Any] | None = None,
                 provider=None, alert: Callable[[str, str, str], None] | None = None, clock: Callable[[], datetime] | None = None,
                 max_symbols: int = 30, store=None):
        self.symbols = [s.upper() for s in symbols]
        self.calendar, self.bus, self.feed, self.factory, self.provider, self.store = calendar, bus, feed, stream_factory, provider, store
        self.alert = alert or (lambda title, msg, level="info": None)
        self.clock = clock or (lambda: datetime.now(NY))
        if len(self.symbols) > max_symbols:
            raise ValueError(f"{len(self.symbols)} symbols exceed the {max_symbols}-symbol subscription cap of this plan")
        self.quote_symbols: set[str] = set()
        self.last_bar: dict[str, datetime] = {}
        self.seen: dict[str, deque] = {}
        self.stats = {"bars": 0, "duplicates": 0, "out_of_order": 0, "quotes": 0, "backfilled": 0, "reconnects": 0, "errors": 0}
        self.connected = False
        self.fatal: str | None = None
        self.connected_at: datetime | None = None
        self.disconnected_at: datetime | None = None
        self._ever_connected = False
        self._stream = None
        self._thread: threading.Thread | None = None
        self.agg = Aggregator30m(calendar)
        self._lock = threading.Lock()

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("market data hub already started: a second connection is refused (one connection per account on this plan)")
        if self.fatal:
            raise RuntimeError(f"hub is in a fatal state: {self.fatal}")
        if self.factory is None:
            raise RuntimeError("no stream factory configured")
        self._stream = self.factory(self)
        self._stream.subscribe_bars(self._bar_handler, *self.symbols)
        if self.quote_symbols:
            self._stream.subscribe_quotes(self._quote_handler, *sorted(self.quote_symbols))
        self._thread = threading.Thread(target=self._stream.run, name="marketdata", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("stream stop: %s", e)
        self.connected = False

    async def _bar_handler(self, msg):
        self.on_bar_raw(msg)

    async def _quote_handler(self, msg):
        self.on_quote_raw(msg)

    # --------------------------------------------------------------- callbacks
    def on_connected(self) -> None:
        with self._lock:
            reconnect = self._ever_connected
            self._ever_connected, self.connected, self.connected_at = True, True, self.clock()
        if reconnect:
            self.stats["reconnects"] += 1
            self.alert("market data reconnected", f"reconnect #{self.stats['reconnects']}; backfilling the gap from REST", "warning")
            self.backfill_gaps()
        if self.store is not None:
            self.store.heartbeat("marketdata", self.clock(), "connected")

    def on_disconnected(self) -> None:
        with self._lock:
            self.connected, self.disconnected_at = False, self.clock()

    def on_error(self, code: Any, msg: Any) -> None:
        self.stats["errors"] += 1
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            code_i = -1
        if code_i == CODE_CONNECTION_LIMIT:
            self.fatal = f"406 connection limit exceeded: {msg}. Another process holds the market-data connection; not retrying."
            log.error(self.fatal)
            self.alert("market data: connection limit (406)", self.fatal, "critical")
            self.stop()
            return
        log.error("market data stream error %s: %s", code, msg)

    def on_bar_raw(self, msg: dict[str, Any], *, backfilled: bool = False) -> BarEvent | None:
        sym = str(msg.get("S") or msg.get("symbol") or "").upper()
        if not sym:
            return None
        ts = _parse_ts(msg.get("t") or msg.get("timestamp"))
        sess = self.calendar.session(ts.date())
        last = self.last_bar.get(sym)
        if last is not None and ts == last:
            self.stats["duplicates"] += 1
            return None
        if last is not None and ts < last:
            self.stats["out_of_order"] += 1
            log.warning("out-of-order bar dropped: %s %s < %s", sym, ts, last)
            return None
        ev = BarEvent(sym, ts, float(msg.get("o", msg.get("open"))), float(msg.get("h", msg.get("high"))), float(msg.get("l", msg.get("low"))),
                      float(msg.get("c", msg.get("close"))), float(msg.get("v", msg.get("volume", 0))), "1m", ts.date(),
                      is_session_end=bool(sess and ts + timedelta(minutes=1) >= sess.close), vwap=float(msg["vw"]) if msg.get("vw") is not None else None,
                      trade_count=float(msg["n"]) if msg.get("n") is not None else None, backfilled=backfilled)
        self.last_bar[sym] = ts
        self.stats["bars"] += 1
        if backfilled:
            self.stats["backfilled"] += 1
        self.bus.publish(ev)
        if self.store is not None:
            self.store.set_meta("last_bar_ts", ts.isoformat())
        ev30 = self.agg.add(ev)
        if ev30 is not None:
            self.bus.publish(ev30)
        return ev

    def on_quote_raw(self, msg: dict[str, Any]) -> QuoteEvent | None:
        sym = str(msg.get("S") or msg.get("symbol") or "").upper()
        if not sym:
            return None
        ev = QuoteEvent(sym, _parse_ts(msg.get("t") or msg.get("timestamp")), float(msg.get("bp", msg.get("bid_price", 0)) or 0),
                        float(msg.get("ap", msg.get("ask_price", 0)) or 0), float(msg.get("bs", 0) or 0), float(msg.get("as", 0) or 0))
        self.stats["quotes"] += 1
        self.bus.publish(ev)
        return ev

    # --------------------------------------------------------------- freshness
    def last_bar_age(self, symbol: str, now: datetime | None = None) -> float | None:
        last = self.last_bar.get(symbol.upper())
        if last is None:
            return None
        return ((now or self.clock()) - (last + timedelta(minutes=1))).total_seconds()

    # ------------------------------------------------------------ subscriptions
    def set_quote_symbols(self, symbols: set[str]) -> tuple[set[str], set[str]]:
        want = {s.upper() for s in symbols} & set(self.symbols)
        add, remove = want - self.quote_symbols, self.quote_symbols - want
        self.quote_symbols = want
        if self._stream is not None and self.connected:
            try:
                if add:
                    self._stream.subscribe_quotes(self._quote_handler, *sorted(add))
                if remove:
                    self._stream.unsubscribe_quotes(*sorted(remove))
            except Exception as e:  # noqa: BLE001
                log.warning("quote subscription update failed: %s", e)
        return add, remove

    # ---------------------------------------------------------------- backfill
    def backfill_gaps(self, now: datetime | None = None) -> int:
        """After a reconnect: fetch the missing minutes per symbol from REST (SIP for what is older than the lag,
        IEX for the rest) and publish them as backfilled bars through the same dedup/order path."""
        if self.provider is None:
            return 0
        now = now or self.clock()
        n = 0
        for sym in self.symbols:
            last = self.last_bar.get(sym)
            if last is None:
                continue
            start = last + timedelta(minutes=1)
            if start >= now - timedelta(minutes=1):
                continue
            frames = []
            sip_end = now - SIP_LAG
            try:
                if start < sip_end:
                    frames.append(self.provider.fetch_minute(sym, start, sip_end, feed="sip", now=now))
                    start = sip_end
            except Exception as e:  # noqa: BLE001
                log.warning("SIP backfill for %s failed (%s); using IEX for the whole gap", sym, type(e).__name__)
                start = last + timedelta(minutes=1)
            try:
                frames.append(self.provider.fetch_minute(sym, start, now, feed="iex", now=now))
            except Exception as e:  # noqa: BLE001
                log.error("IEX backfill for %s failed: %s", sym, e)
                self.alert("backfill failed", f"{sym}: {type(e).__name__}: {e}", "warning")
            for df in frames:
                for ts, r in df.iterrows():
                    ev = self.on_bar_raw({"S": sym, "t": ts.to_pydatetime(), "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"], "v": r["volume"],
                                          "vw": r.get("vwap")}, backfilled=True)
                    n += 1 if ev is not None else 0
        return n
