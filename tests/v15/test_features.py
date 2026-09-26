"""FeatureEngine: incremental features from 1m/30m/1d bars, no lookahead, earnings and synthetic flags."""
import math
from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from bot.core.events import BarEvent, QuoteEvent
from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from bot.features.engine import FeatureEngine, FeatureSnapshot, load_earnings
from tests.v15.helpers import minute_history

CAL = SessionCalendar()


def feed(fe: FeatureEngine, df, symbol="SPY"):
    """Feed 1m bars and 30m aggregates exactly as the minute engine does."""
    acc = []
    for ts, r in df.iterrows():
        ts = ts.to_pydatetime()
        sess = CAL.session(ts.date())
        end = ts + timedelta(minutes=1) >= sess.close
        ev = BarEvent(symbol, ts, r.open, r.high, r.low, r.close, r.volume, "1m", ts.date(), is_session_end=end, vwap=r.get("vwap"))
        fe.on_bar(ev)
        acc.append(ev)
        if ((ts - sess.open).total_seconds() // 60 + 1) % 30 == 0 or end:
            b0 = acc[0]
            fe.on_bar(BarEvent(symbol, b0.ts, b0.open, max(x.high for x in acc), min(x.low for x in acc), acc[-1].close, sum(x.volume for x in acc), "30m",
                               ts.date(), is_session_end=end, partial=len(acc) < 30 and end))
            acc = []


SESSIONS = [s.date for s in CAL.sessions_between(date(2026, 6, 1), date(2026, 9, 25))]


def test_snapshot_before_any_data_is_empty():
    fe = FeatureEngine()
    s = fe.snapshot("SPY", datetime(2026, 9, 25, 10, 0, tzinfo=NY))
    assert isinstance(s, FeatureSnapshot) and s.last_close is None and s.daily_bars == 0 and s.atr14_d is None
    assert s.to_dict()["asof"].startswith("2026-09-25")


def test_session_features_r1_r12_vwap_and_daily_rollup():
    df = minute_history(SESSIONS[-3:])
    fe = FeatureEngine()
    feed(fe, df)
    last = SESSIONS[-1]
    day = df[df.index.date == last]
    s = fe.snapshot("SPY", datetime.combine(last, time(16, 0), NY))
    prev = df[df.index.date == SESSIONS[-2]]["close"].iloc[-1]
    assert s.prev_session_close == pytest.approx(prev)
    assert s.session_open == pytest.approx(day["open"].iloc[0]) and s.last_close == pytest.approx(day["close"].iloc[-1])
    c0930 = day.between_time("09:30", "09:59")["close"].iloc[-1]
    assert s.r1 == pytest.approx(math.log(c0930 / prev))
    c1500, c1530 = day.between_time("14:30", "14:59")["close"].iloc[-1], day.between_time("15:00", "15:29")["close"].iloc[-1]
    assert s.r12 == pytest.approx(math.log(c1530 / c1500))
    assert s.session_vwap == pytest.approx((day["vwap"] * day["volume"]).sum() / day["volume"].sum())
    assert s.daily_bars == 3 and s.session_volume == pytest.approx(day["volume"].sum())
    assert s.vol_1m_21 is not None and s.vol_1m_21 > 0 and s.vol_30m_21 is not None


def test_between_sessions_snapshot_reports_previous_session_close():
    df = minute_history(SESSIONS[-2:])
    fe = FeatureEngine()
    feed(fe, df[df.index.date == SESSIONS[-2]])
    s = fe.snapshot("SPY", datetime.combine(SESSIONS[-1], time(9, 30), NY))
    assert s.prev_session_close == pytest.approx(df[df.index.date == SESSIONS[-2]]["close"].iloc[-1])
    assert s.session_open is None and s.r1 is None and s.session_volume == 0.0 and s.session_vwap is None


def test_no_lookahead_snapshot_only_sees_bars_already_fed():
    df = minute_history(SESSIONS[-2:])
    fe = FeatureEngine()
    last = SESSIONS[-1]
    cut = datetime.combine(last, time(11, 0), NY)
    feed(fe, df[df.index < cut])
    s = fe.snapshot("SPY", cut)
    assert s.last_close == pytest.approx(df[df.index < cut]["close"].iloc[-1])
    assert s.r12 is None and s.session_volume == pytest.approx(df[(df.index.date == last) & (df.index < cut)]["volume"].sum())


def test_daily_bars_drive_atr_vol_and_momentum_and_beta():
    fe = FeatureEngine(benchmark="SPY")
    rng = np.random.default_rng(0)
    px_b, px_s = 100.0, 50.0
    d0 = date(2025, 1, 2)
    days = [s.date for s in CAL.sessions_between(d0, date(2026, 9, 25))]
    for i, d in enumerate(days):
        rb = rng.normal(0.0003, 0.01)
        rs = 1.2 * rb + rng.normal(0, 0.005)
        px_b *= math.exp(rb); px_s *= math.exp(rs)
        ts = datetime.combine(d, time(16, 0), NY)
        fe.on_bar(BarEvent("SPY", ts, px_b, px_b * 1.01, px_b * 0.99, px_b, 1e6, "1d", d))
        fe.on_bar(BarEvent("XLK", ts, px_s, px_s * 1.01, px_s * 0.99, px_s, 1e5, "1d", d))
    s = fe.snapshot("XLK", datetime.combine(days[-1], time(16, 0), NY))
    assert s.daily_bars == min(len(days), 300) and s.atr14_d is not None and s.atr14_d > 0
    assert s.vol_d_21 is not None and s.vol_d_63 is not None and 0.05 < s.vol_d_21 < 0.5   # annualised
    assert s.beta_60d is not None and 0.8 < s.beta_60d < 1.6
    assert s.sigma_res_21d is not None and s.res5 is not None
    assert s.ret_12m_ex_1m is not None


def test_earnings_flag_and_synthetic_fraction_and_quote_spread(tmp_path):
    p = tmp_path / "earnings.csv"
    p.write_text("symbol,date\nAAPL,2026-09-29\nMSFT,2026-12-01\n")
    earn = load_earnings(p)
    assert earn == {"AAPL": {date(2026, 9, 29)}, "MSFT": {date(2026, 12, 1)}}
    fe = FeatureEngine(earnings=earn, calendar=CAL)
    d = SESSIONS[-1]
    ts = datetime.combine(d, time(10, 0), NY)
    for i in range(30):
        t = ts + timedelta(minutes=i)
        fe.on_bar(BarEvent("AAPL", t, 100, 100.1, 99.9, 100, 1000, "1m", d, backfilled=(i % 3 == 0)))
    fe.on_quote(QuoteEvent("AAPL", ts + timedelta(minutes=29), 99.99, 100.01))
    s = fe.snapshot("AAPL", ts + timedelta(minutes=30))
    assert s.earnings_within_5 is True
    assert s.synthetic_frac_30m == pytest.approx(10 / 30)
    assert s.spread_bps == pytest.approx(2.0, rel=1e-3) and s.spread_age_s == pytest.approx(60)
    s2 = fe.snapshot("MSFT", ts)
    assert s2.earnings_within_5 is False


def test_relvol_1515_is_ratio_to_median_of_history():
    from statistics import median
    df = minute_history(SESSIONS[-12:])
    last = SESSIONS[-1]
    cut = datetime.combine(last, time(15, 15), NY)
    fe = FeatureEngine()
    feed(fe, df[df.index < cut])
    s = fe.snapshot("SPY", cut)
    vol_to = df[(df.index.date == last) & (df.index < cut)]["volume"].sum()
    hist = [df[df.index.date == d].between_time("09:30", "15:14")["volume"].sum() for d in SESSIONS[-12:-1]]
    assert s.relvol_1515 == pytest.approx(vol_to / median(hist), rel=1e-6)
    # too little history -> None, never a guess
    fe2 = FeatureEngine()
    feed(fe2, minute_history(SESSIONS[-3:]))
    assert fe2.snapshot("SPY", cut).relvol_1515 is None
