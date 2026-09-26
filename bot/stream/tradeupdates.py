"""TradeUpdatesClient (spec §9): the account's ``trade_updates`` stream, host-verified per environment, every event
converted to a TradeUpdateEvent and published on the bus (the OMS applies them idempotently). On every reconnect the
daemon reconciles orders since the watermark. Paper sends binary frames; alpaca-py decodes both."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from bot.core.events import TradeUpdateEvent
from bot.data.calendar import NY

log = logging.getLogger(__name__)


def parse_trade_update(msg: Any) -> TradeUpdateEvent | None:
    """Accepts the raw frame ({"stream": "trade_updates", "data": {...}}), the data dict, or the SDK's TradeUpdate model."""
    if msg is None:
        return None
    if not isinstance(msg, dict):
        d = {"event": getattr(msg, "event", None), "timestamp": getattr(msg, "timestamp", None), "price": getattr(msg, "price", None),
             "qty": getattr(msg, "qty", None), "order": getattr(msg, "order", None)}
        o = d["order"]
        if o is not None and not isinstance(o, dict):
            o = {k: getattr(o, k, None) for k in ("id", "client_order_id", "symbol", "side", "qty", "filled_qty", "filled_avg_price", "status")}
        d["order"] = o
    else:
        d = msg.get("data", msg)
    o = d.get("order") or {}
    ev = str(d.get("event") or "")
    if not ev or not o:
        return None

    def f(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None
    ts_raw = d.get("timestamp") or o.get("updated_at") or o.get("filled_at")
    if isinstance(ts_raw, datetime):
        ts = ts_raw if ts_raw.tzinfo else ts_raw.replace(tzinfo=timezone.utc)
    elif ts_raw:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)
    side = str(getattr(o.get("side"), "value", o.get("side")) or "").lower()
    status = str(getattr(o.get("status"), "value", o.get("status")) or "").lower()
    price = f(d.get("price")) or f(o.get("filled_avg_price"))
    return TradeUpdateEvent(str(o.get("id")), str(o.get("client_order_id")), ev, ts.astimezone(NY), str(o.get("symbol")), side, f(o.get("qty")) or 0.0,
                            f(o.get("filled_qty")) or 0.0, price, status, raw={"leg_qty": f(d.get("qty")), "position_qty": f(d.get("position_qty"))})


def make_trading_stream_factory(settings, env: str) -> Callable[["TradeUpdatesClient"], Any]:
    def factory(client: "TradeUpdatesClient"):
        key, secret = settings.credentials(env)
        return _ClientStream(client, api_key=key, secret_key=secret, paper=(env == "paper"), raw_data=True)
    return factory


try:
    from alpaca.trading.stream import TradingStream as _Base
except Exception:  # pragma: no cover
    _Base = object


class _ClientStream(_Base):  # type: ignore[misc]
    def __init__(self, client: "TradeUpdatesClient", **kw):
        super().__init__(**kw)
        self._client = client

    async def _start_ws(self):
        await super()._start_ws()
        self._client.on_connected()

    async def close(self):
        await super().close()
        self._client.on_disconnected()


def verify_stream_host(stream, env: str) -> str:
    ep = getattr(stream, "_endpoint", "")
    url = str(getattr(ep, "value", ep))
    if env == "paper" and "paper-api" not in url:
        raise RuntimeError(f"paper env requested but the trade-updates endpoint is {url!r}")
    if env == "live" and "paper-api" in url:
        raise RuntimeError(f"live env requested but the trade-updates endpoint is {url!r}")
    return url


class TradeUpdatesClient:
    def __init__(self, *, env: str, bus, stream_factory: Callable[["TradeUpdatesClient"], Any] | None = None, store=None,
                 alert: Callable[[str, str, str], None] | None = None, on_reconnect: Callable[[], None] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.env, self.bus, self.factory, self.store = env, bus, stream_factory, store
        self.alert = alert or (lambda title, msg, level="info": None)
        self.on_reconnect = on_reconnect
        self.clock = clock or (lambda: datetime.now(NY))
        self.connected = False
        self._ever_connected = False
        self.reconnects = 0
        self.events = 0
        self.watermark: datetime | None = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self.endpoint = ""

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("trade updates client already started")
        if self.factory is None:
            raise RuntimeError("no stream factory configured")
        self._stream = self.factory(self)
        self.endpoint = verify_stream_host(self._stream, self.env)
        self._stream.subscribe_trade_updates(self._handler)
        self._thread = threading.Thread(target=self._stream.run, name="tradeupdates", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("trade updates stop: %s", e)
        self.connected = False

    async def _handler(self, msg):
        self.on_raw(msg)

    def on_raw(self, msg: Any) -> TradeUpdateEvent | None:
        ev = parse_trade_update(msg)
        if ev is None:
            log.warning("unparseable trade update: %r", msg)
            return None
        self.events += 1
        self.watermark = max(self.watermark, ev.ts) if self.watermark else ev.ts
        self.bus.publish(ev)
        if self.store is not None:
            self.store.set_watermark("trade_updates", ev.ts)
        return ev

    def on_connected(self) -> None:
        reconnect = self._ever_connected
        self._ever_connected, self.connected = True, True
        if self.store is not None:
            self.store.heartbeat("trade_updates", self.clock(), "connected")
        if reconnect:
            self.reconnects += 1
            self.alert("trade updates reconnected", f"reconnect #{self.reconnects}; reconciling orders since the watermark", "warning")
            if self.on_reconnect is not None:
                self.on_reconnect()

    def on_disconnected(self) -> None:
        self.connected = False
        if self.store is not None:
            self.store.heartbeat("trade_updates", self.clock(), "disconnected")
