from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bot.data.calendar import NY
from bot.data.minute import aggregate, fetch_minute_history
from bot.data.providers import DataPlanError, alpaca_request_window, is_subscription_error, sip_lagged_end
from bot.data.quality import check_minute_frame
from bot.data.sessions import SessionCalendar
from bot.data.store import BarStore

CAL = SessionCalendar()


def minute_bars(d: date, *, start=(9, 30), minutes=390, seed=0, base=500.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(datetime(d.year, d.month, d.day, *start, tzinfo=NY), periods=minutes, freq="1min")
    close = base + np.cumsum(rng.normal(0, 0.05, minutes))
    return pd.DataFrame({"open": close - 0.01, "high": close + 0.03, "low": close - 0.03, "close": close,
                         "volume": rng.integers(100, 1000, minutes).astype(float), "trade_count": 10.0, "vwap": close}, index=idx)


def test_aggregate_30m_boundaries_regular_session():
    d = date(2026, 9, 25)
    df = minute_bars(d)
    agg = aggregate(df, 30, CAL)
    assert len(agg) == 13
    assert agg.index[0] == datetime(2026, 9, 25, 9, 30, tzinfo=NY) and agg.index[-1] == datetime(2026, 9, 25, 15, 30, tzinfo=NY)
    assert (agg["n_minutes"] == 30).all() and not agg["partial"].any()
    assert agg["is_session_end"].tolist() == [False] * 12 + [True]
    first = df.iloc[:30]
    assert agg["open"].iloc[0] == first["open"].iloc[0] and agg["close"].iloc[0] == first["close"].iloc[-1]
    assert agg["high"].iloc[0] == first["high"].max() and agg["volume"].iloc[0] == first["volume"].sum()


def test_aggregate_early_close_partial_bar_and_session_end():
    d = date(2024, 11, 29)   # 13:00 close
    df = minute_bars(d, minutes=210)
    agg30 = aggregate(df, 30, CAL)
    assert len(agg30) == 7 and not agg30["partial"].any() and agg30["is_session_end"].iloc[-1]
    agg60 = aggregate(df, 60, CAL)
    assert len(agg60) == 4
    last = agg60.iloc[-1]
    assert last["partial"] and last["is_session_end"] and last["n_minutes"] == 30
    assert agg60.index[-1] == datetime(2024, 11, 29, 12, 30, tzinfo=NY)


def test_aggregate_drops_holiday_and_out_of_session_bars():
    holiday = minute_bars(date(2024, 6, 19))                       # Juneteenth: no session
    pre = minute_bars(date(2026, 9, 25), start=(8, 0), minutes=90)  # pre-market
    post = minute_bars(date(2026, 9, 25), start=(16, 0), minutes=30)
    good = minute_bars(date(2026, 9, 25))
    agg = aggregate(pd.concat([holiday, pre, good, post]), 30, CAL)
    assert len(agg) == 13 and set(agg["session_date"]) == {date(2026, 9, 25)}


def test_aggregate_across_dst_transition_days():
    for d in (date(2026, 3, 9), date(2026, 11, 2)):
        df = minute_bars(d)
        agg = aggregate(df, 30, CAL)
        assert len(agg) == 13 and agg.index[0].hour == 9 and agg.index[0].minute == 30, d
        # stored/aggregated in UTC-naive form the offsets differ (14:30Z in EDT vs 15:30Z in EST) but the ET clock is fixed
        assert agg.index[0].tz_convert("UTC").hour == (13 if d.month == 3 else 14)   # 09:30 EDT = 13:30Z, 09:30 EST = 14:30Z


def test_sip_lag_boundary_and_iex_no_lag():
    now = datetime(2026, 9, 25, 15, 0, tzinfo=NY)
    end = datetime(2026, 9, 25, 15, 0, tzinfo=NY)
    assert sip_lagged_end(end, now, feed="sip") == now - timedelta(minutes=16)
    assert sip_lagged_end(end, now, feed="iex") == end
    old = datetime(2026, 9, 24, 16, 0, tzinfo=NY)
    assert sip_lagged_end(old, now, feed="sip") == old


def test_subscription_error_detection():
    class E(Exception):
        status_code = 403
    assert is_subscription_error(Exception("subscription does not permit querying recent SIP data"))
    assert is_subscription_error(E("your plan does not permit this"))
    assert not is_subscription_error(Exception("rate limit"))
    with pytest.raises(DataPlanError):
        raise DataPlanError("x")


class ScriptedMinuteProvider:
    """Serves minute bars from a fixed frame; records requested windows; can refuse recent SIP data like Alpaca Basic."""

    def __init__(self, df: pd.DataFrame, *, refuse_recent_sip: bool = False):
        self.df, self.calls, self.refuse = df, [], refuse_recent_sip

    def fetch_minute(self, symbol, start, end, *, feed="sip", now=None):
        end = sip_lagged_end(end, now, feed=feed)
        if self.refuse and feed == "sip" and end > now - timedelta(minutes=15):
            raise DataPlanError("subscription does not permit")
        self.calls.append((start, end, feed))
        return self.df[(self.df.index >= start) & (self.df.index < end)]


def test_fetch_minute_history_is_resumable_and_marks_completeness():
    sessions = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
    frames = [minute_bars(d, seed=i) for i, d in enumerate(sessions)]
    frames[2] = frames[2].iloc[:300]     # 2026-09-23 has 90 minutes missing -> incomplete
    df = pd.concat(frames)
    prov = ScriptedMinuteProvider(df)
    store = BarStore()
    now = datetime(2026, 9, 25, 17, 0, tzinfo=NY)
    s1 = fetch_minute_history(store, prov, "SPY", date(2026, 9, 21), date(2026, 9, 25), feed="sip", calendar=CAL, chunk_sessions=2, now=now)
    assert s1.sessions_requested == 5 and s1.sessions_fetched == 5 and s1.bars_written == len(df)
    assert s1.incomplete == [date(2026, 9, 23)]
    cov = store.minute_coverage("SPY", "sip")
    assert cov[date(2026, 9, 21)] == (390, True) and cov[date(2026, 9, 23)] == (300, False)
    n_calls = len(prov.calls)
    s2 = fetch_minute_history(store, prov, "SPY", date(2026, 9, 21), date(2026, 9, 25), feed="sip", calendar=CAL, now=now)
    assert s2.sessions_fetched == 1, "only the incomplete session is re-requested"
    assert len(prov.calls) == n_calls + 1
    out = store.get_minute_bars("SPY", feed="sip")
    assert len(out) == len(df) and str(out.index.tz) == "America/New_York"


def test_fetch_current_session_sip_not_final_until_16_minutes_after_close():
    d = date(2026, 9, 25)
    df = minute_bars(d)
    prov = ScriptedMinuteProvider(df)
    store = BarStore()
    s = fetch_minute_history(store, prov, "SPY", d, d, feed="sip", calendar=CAL, now=datetime(2026, 9, 25, 16, 5, tzinfo=NY))
    assert store.minute_coverage("SPY", "sip")[d][1] is False and s.incomplete == []   # not final yet, not an error
    s = fetch_minute_history(store, prov, "SPY", d, d, feed="sip", calendar=CAL, now=datetime(2026, 9, 25, 16, 20, tzinfo=NY))
    assert store.minute_coverage("SPY", "sip")[d] == (390, True)


def _alpaca_provider_with_stub(response=None, raise_exc=None):
    from bot.config import Settings
    from bot.data.providers import AlpacaBarProvider
    prov = AlpacaBarProvider(Settings(_env_file=None, alpaca_paper_api_key="PKTESTX", alpaca_paper_secret_key="s" * 20), env="paper")
    calls = []

    class Stub:
        def get_stock_bars(self, req):
            calls.append(req)
            if raise_exc is not None:
                raise raise_exc
            return response
    prov.client = Stub()
    return prov, calls


def test_dataplan_error_is_raised_not_swallowed():
    """The real provider path: Alpaca answers a SIP request with the subscription error -> DataPlanError, no IEX fallback."""
    from alpaca.common.exceptions import APIError
    exc = APIError('{"code":40310000,"message":"subscription does not permit querying recent SIP data"}')
    prov, calls = _alpaca_provider_with_stub(raise_exc=exc)
    with pytest.raises(DataPlanError):
        prov.fetch_minute("SPY", datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), feed="sip",
                          now=datetime(2026, 9, 25, 16, 5, tzinfo=NY))
    assert len(calls) == 1, "exactly one attempt; never a silent fallback to another feed"
    # other API errors propagate unchanged
    prov, _ = _alpaca_provider_with_stub(raise_exc=APIError('{"code":42910000,"message":"rate limited"}'))
    with pytest.raises(APIError):
        prov.fetch_minute("SPY", datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), feed="iex")


def test_alpaca_minute_fetch_applies_lag_and_converts_timestamps():
    """A BarSet-like response with a (symbol, timestamp) UTC MultiIndex becomes NY bar-start timestamps."""
    d = date(2026, 9, 25)
    df = minute_bars(d)
    utc = df.copy()
    utc.index = pd.MultiIndex.from_arrays([["SPY"] * len(df), df.index.tz_convert("UTC")], names=["symbol", "timestamp"])
    prov, calls = _alpaca_provider_with_stub(response=SimpleNamespace(df=utc))
    now = datetime(2026, 9, 25, 15, 0, tzinfo=NY)
    out = prov.fetch_minute("SPY", datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), feed="sip", now=now)
    assert str(out.index.tz) == "America/New_York" and out.index[0] == datetime(2026, 9, 25, 9, 30, tzinfo=NY)
    req = calls[0]
    assert req.end.astimezone(NY) == now - timedelta(minutes=16), "SIP end lagged by 16 minutes"
    prov2, calls2 = _alpaca_provider_with_stub(response=SimpleNamespace(df=utc))
    prov2.fetch_minute("SPY", datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 14, 0, tzinfo=NY), feed="iex", now=now)
    assert calls2[0].end.astimezone(NY) == datetime(2026, 9, 25, 14, 0, tzinfo=NY), "IEX is not lagged"


# ---------------------------------------------------------------- request datetime convention (aware UTC at the API boundary)
UTC = timezone.utc
TOKYO = ZoneInfo("Asia/Tokyo")


def _lag_request(start, end, now, feed="sip"):
    prov, calls = _alpaca_provider_with_stub(response=SimpleNamespace(df=pd.DataFrame()))
    prov.fetch_minute("SPY", start, end, feed=feed, now=now)
    return calls[0]


def test_request_window_is_aware_utc_for_aware_ny_and_aware_utc_inputs():
    start_ny, end_ny = datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY)
    now = datetime(2026, 9, 25, 15, 0, tzinfo=NY)
    s, e = alpaca_request_window(start_ny, end_ny, now=now, feed="sip")
    assert s.utcoffset() == timedelta(0) and e.utcoffset() == timedelta(0) and s.tzinfo is not None, "aware UTC (ZoneInfo or timezone.utc: same instant)"
    assert s == datetime(2026, 9, 25, 13, 30, tzinfo=UTC) and e == datetime(2026, 9, 25, 18, 44, tzinfo=UTC)
    s2, e2 = alpaca_request_window(start_ny.astimezone(UTC), end_ny.astimezone(UTC), now=now.astimezone(UTC), feed="sip")
    assert (s2, e2) == (s, e), "aware UTC inputs describe the same instants"
    s3, e3 = alpaca_request_window(start_ny.astimezone(TOKYO), end_ny.astimezone(TOKYO), now=now.astimezone(TOKYO), feed="sip")
    assert (s3, e3) == (s, e), "any aware zone describes the same instants"
    s4, e4 = alpaca_request_window(datetime(2026, 9, 25, 9, 30), datetime(2026, 9, 25, 16, 0), now=now, feed="sip")
    assert (s4, e4) == (s, e), "naive inputs are New York wall clock by convention"


def test_request_window_handles_dst_summer_and_winter():
    summer = alpaca_request_window(datetime(2026, 7, 6, 9, 30, tzinfo=NY), datetime(2026, 7, 6, 16, 0, tzinfo=NY), now=datetime(2026, 7, 7, tzinfo=NY), feed="iex")
    winter = alpaca_request_window(datetime(2026, 12, 7, 9, 30, tzinfo=NY), datetime(2026, 12, 7, 16, 0, tzinfo=NY), now=datetime(2026, 12, 8, tzinfo=NY), feed="iex")
    assert summer == (datetime(2026, 7, 6, 13, 30, tzinfo=UTC), datetime(2026, 7, 6, 20, 0, tzinfo=UTC))       # EDT = UTC-4
    assert winter == (datetime(2026, 12, 7, 14, 30, tzinfo=UTC), datetime(2026, 12, 7, 21, 0, tzinfo=UTC))     # EST = UTC-5


def test_request_window_lag_rules():
    now = datetime(2026, 9, 25, 15, 0, tzinfo=NY)
    _, e = alpaca_request_window(datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), now=now, feed="sip")
    assert e == datetime(2026, 9, 25, 14, 44, tzinfo=NY), "SIP end is now - 16 minutes"
    _, e = alpaca_request_window(datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 14, 0, tzinfo=NY), now=now, feed="sip")
    assert e == datetime(2026, 9, 25, 14, 0, tzinfo=NY), "an end earlier than the cutoff is kept"
    _, e = alpaca_request_window(datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), now=now, feed="iex")
    assert e == datetime(2026, 9, 25, 16, 0, tzinfo=NY), "IEX inherits no SIP lag"


def test_sdk_request_fields_stay_aware_and_serialize_the_same_instant():
    now = datetime(2026, 9, 25, 15, 0, tzinfo=NY)
    req = _lag_request(datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), now)
    assert req.start.tzinfo is not None and req.end.tzinfo is not None, "request bounds are timezone-aware on the SDK object"
    assert req.start == datetime(2026, 9, 25, 13, 30, tzinfo=UTC) and req.end == datetime(2026, 9, 25, 18, 44, tzinfo=UTC)
    fields = req.to_request_fields()
    assert fields["start"] == "2026-09-25T13:30:00+00:00" and fields["end"] == "2026-09-25T18:44:00+00:00", "wire format carries the instant"
    # the same request built with UTC inputs is identical on the wire
    req2 = _lag_request(datetime(2026, 9, 25, 13, 30, tzinfo=UTC), datetime(2026, 9, 25, 20, 0, tzinfo=UTC), now.astimezone(UTC))
    assert req2.to_request_fields()["end"] == fields["end"] and req2.end == req.end
    # a request whose end is earlier than the lag cutoff is not lagged; an empty window makes no call
    prov, calls = _alpaca_provider_with_stub(response=SimpleNamespace(df=pd.DataFrame()))
    out = prov.fetch_minute("SPY", datetime(2026, 9, 25, 14, 50, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), feed="sip", now=now)
    assert out.empty and calls == [], "start after the SIP cutoff: nothing to request"


def test_request_instants_do_not_depend_on_the_host_timezone(monkeypatch):
    import time as _time
    now = datetime(2026, 9, 25, 15, 0, tzinfo=NY)
    results = {}
    for tz in ("UTC", "America/New_York", "Asia/Tokyo"):
        monkeypatch.setenv("TZ", tz)
        _time.tzset()
        req = _lag_request(datetime(2026, 9, 25, 9, 30, tzinfo=NY), datetime(2026, 9, 25, 16, 0, tzinfo=NY), now)
        results[tz] = (req.start.astimezone(NY), req.end.astimezone(NY), req.to_request_fields()["end"])
    monkeypatch.delenv("TZ", raising=False)
    _time.tzset()
    assert len(set(results.values())) == 1, results
    assert results["UTC"][1] == datetime(2026, 9, 25, 14, 44, tzinfo=NY)


def test_minute_quality_detects_missing_duplicates_and_outside_session():
    d = date(2026, 9, 25)
    df = minute_bars(d)
    df = df.drop(df.index[100:112])                  # 12 missing minutes (3% > 2% tolerance)
    dup = pd.concat([df, df.iloc[[5]]])              # a duplicate timestamp
    pre = minute_bars(d, start=(9, 0), minutes=10)   # outside session
    rep = check_minute_frame("SPY", pd.concat([dup, pre]).sort_index(), CAL, feed="sip")
    assert rep.duplicates == 1 and rep.outside_session == 10
    assert rep.missing_minutes[d] == 12 and rep.incomplete_sessions == [d] and not rep.ok
    clean = check_minute_frame("SPY", minute_bars(d), CAL)
    assert clean.ok and clean.sessions == 1


def test_minute_quality_flags_split_discontinuity_and_zero_volume_runs():
    a = minute_bars(date(2026, 9, 24), base=500)
    b = minute_bars(date(2026, 9, 25), base=250)     # 2:1 split unadjusted
    b.iloc[50:60, b.columns.get_loc("volume")] = 0
    rep = check_minute_frame("X", pd.concat([a, b]), CAL)
    assert rep.split_discontinuities and rep.split_discontinuities[0][0] == date(2026, 9, 25)
    assert rep.zero_volume_runs.get(date(2026, 9, 25)) == 10


def test_store_minute_bars_roundtrip_and_quotes():
    store = BarStore()
    d = date(2026, 9, 25)
    df = minute_bars(d)
    assert store.upsert_minute_bars("spy", df, feed="iex") == 390
    assert store.upsert_minute_bars("spy", df, feed="iex", backfilled=True) == 390   # idempotent upsert
    out = store.get_minute_bars("SPY", datetime(2026, 9, 25, 10, 0, tzinfo=NY), datetime(2026, 9, 25, 10, 30, tzinfo=NY), feed="iex")
    assert len(out) == 30 and out["backfilled"].all()
    q = pd.DataFrame({"bid": [499.9], "ask": [500.1], "bid_size": [100.0], "ask_size": [200.0]}, index=pd.DatetimeIndex([datetime(2026, 9, 25, 10, 0, tzinfo=NY)]))
    assert store.upsert_quotes("SPY", q, feed="sip") == 1
    got = store.get_quotes("SPY", feed="sip")
    assert len(got) == 1 and got["ask"].iloc[0] == 500.1
    assert store.con.execute("SELECT count(*) FROM bars_1d").fetchone()[0] == 0   # view over the daily table exists
