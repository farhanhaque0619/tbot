from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.core.bus import EventBus
from bot.core.events import TradeUpdateEvent
from bot.execution.store import ExecutionStore
from bot.stream.tradeupdates import TradeUpdatesClient, parse_trade_update, verify_stream_host

RAW = {"stream": "trade_updates", "data": {"event": "fill", "timestamp": "2026-09-22T14:31:02.123Z", "price": "100.25", "qty": "10", "position_qty": "10",
                                            "order": {"id": "oid-1", "client_order_id": "r-A-SPY-2026-09-22-1-entry", "symbol": "SPY", "side": "buy", "qty": "10",
                                                      "filled_qty": "10", "filled_avg_price": "100.25", "status": "filled"}}}


def test_parse_frame_data_and_model_shapes():
    ev = parse_trade_update(RAW)
    assert isinstance(ev, TradeUpdateEvent) and ev.order_id == "oid-1" and ev.event == "fill" and ev.price == 100.25 and ev.filled_qty == 10
    assert ev.ts.tzinfo is not None and ev.ts.hour == 10 and ev.raw["leg_qty"] == 10.0 and ev.key == ("oid-1", "fill", ev.ts.isoformat())
    assert parse_trade_update(RAW["data"]).key == ev.key
    model = SimpleNamespace(event="partial_fill", timestamp=datetime(2026, 9, 22, 14, 31, tzinfo=timezone.utc), price=100.1, qty=4,
                            order=SimpleNamespace(id="oid-2", client_order_id="c2", symbol="SPY", side="sell", qty=10, filled_qty=4, filled_avg_price=100.1, status="partially_filled"))
    ev2 = parse_trade_update(model)
    assert ev2.event == "partial_fill" and ev2.side == "sell" and ev2.filled_qty == 4
    assert parse_trade_update({"stream": "listening"}) is None and parse_trade_update(None) is None


def test_client_publishes_tracks_watermark_and_reconciles_on_reconnect(tmp_path):
    bus = EventBus()
    st = ExecutionStore(tmp_path / "x.sqlite")
    calls, alerts = [], []
    c = TradeUpdatesClient(env="paper", bus=bus, store=st, on_reconnect=lambda: calls.append(1), alert=lambda t, m, level="info": alerts.append((t, level)))
    c.on_connected()
    assert c.connected and st.heartbeats()["trade_updates"][1] == "connected" and calls == []
    ev = c.on_raw(RAW)
    assert bus.drain() == [ev] and c.watermark == ev.ts and st.watermark("trade_updates") == ev.ts.isoformat()
    c.on_disconnected()
    assert not c.connected and st.heartbeats()["trade_updates"][1] == "disconnected"
    c.on_connected()
    assert calls == [1] and c.reconnects == 1 and ("trade updates reconnected", "warning") in alerts
    assert c.on_raw({"stream": "listening"}) is None


def test_stream_host_verification():
    assert "paper-api" in verify_stream_host(SimpleNamespace(_endpoint="wss://paper-api.alpaca.markets/stream"), "paper")
    with pytest.raises(RuntimeError):
        verify_stream_host(SimpleNamespace(_endpoint="wss://api.alpaca.markets/stream"), "paper")
    with pytest.raises(RuntimeError):
        verify_stream_host(SimpleNamespace(_endpoint="wss://paper-api.alpaca.markets/stream"), "live")
    from alpaca.trading.stream import TradingStream
    assert "paper-api" in verify_stream_host(TradingStream("PKX", "y", paper=True, raw_data=True), "paper")
