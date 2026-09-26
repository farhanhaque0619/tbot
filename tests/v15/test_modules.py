"""M1/M2/M3 rules in isolation (no engine): weights, signals, filters, exits, styles."""
from datetime import date, datetime, time

import pytest

from bot.core.events import ScheduleEvent
from bot.data.calendar import NY
from bot.features.engine import FeatureSnapshot
from bot.strategies.adapter import PositionView
from bot.strategies.v15 import M1VolTrend, M2IntradayMomentum, M3ResidualReversal

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)


def snap(symbol="SPY", **kw) -> FeatureSnapshot:
    return FeatureSnapshot(symbol, datetime(2026, 9, 22, 16, 0, tzinfo=NY), **kw)


def close_event(d):
    return ScheduleEvent("session_close", datetime.combine(d, time(16, 0), NY), d)


# ------------------------------------------------------------------------------------------------- M1
def test_m1_weight_rule_and_stop():
    m = M1VolTrend()
    w, sigma = m.weight(snap(vol_d_21=0.10, vol_d_63=0.20, ret_12m_ex_1m=0.05))
    assert sigma == 0.20 and w == pytest.approx(0.5)
    assert m.weight(snap(vol_d_21=0.10, vol_d_63=0.12, ret_12m_ex_1m=0.05))[0] == pytest.approx(0.6), "capped at 0.60"
    assert m.weight(snap(vol_d_21=0.10, vol_d_63=0.12, ret_12m_ex_1m=-0.05))[0] == 0.0, "tsmom off"
    assert m.weight(snap(vol_d_21=None, vol_d_63=0.12, ret_12m_ex_1m=0.05)) == (None, None), "not ready -> no guess"
    out = m.on_event(close_event(MON), snap(last_close=500.0, vol_d_21=0.10, vol_d_63=0.20, ret_12m_ex_1m=0.05), PositionView(0.0))
    assert len(out) == 1 and out[0].direction == 1 and out[0].target_weight == pytest.approx(0.5) and out[0].entry_style == "cls"
    assert out[0].protective_stop_price == pytest.approx(500.0 * (1 - 4 * 0.20 / 52 ** 0.5)) and out[0].overnight_ok
    assert out[0].volatility == 0.20 and out[0].signal_strength == pytest.approx(0.5 / 0.6)


def test_m1_emits_on_monday_or_when_the_band_is_crossed_and_exits_when_tsmom_off():
    m = M1VolTrend()
    s = snap(last_close=500.0, vol_d_21=0.10, vol_d_63=0.20, ret_12m_ex_1m=0.05)   # w = 0.5
    assert m.on_event(close_event(TUE), s, PositionView(100.0, 500.0, weight=0.45)) == [], "Tuesday, inside the band"
    assert len(m.on_event(close_event(MON), s, PositionView(100.0, 500.0, weight=0.45))) == 1, "Monday rebalance"
    assert len(m.on_event(close_event(TUE), s, PositionView(100.0, 500.0, weight=0.30))) == 1, "band crossed"
    off = snap(last_close=500.0, vol_d_21=0.10, vol_d_63=0.20, ret_12m_ex_1m=-0.01)
    ex = m.on_event(close_event(TUE), off, PositionView(100.0, 500.0, weight=0.5))
    assert len(ex) == 1 and ex[0].direction == 0 and ex[0].target_weight is None
    assert m.on_event(close_event(TUE), off, PositionView(0.0)) == [], "flat and tsmom off: nothing to do"
    assert m.on_event(ScheduleEvent("pre_open", datetime(2026, 9, 22, 9, 0, tzinfo=NY), TUE), s, PositionView(0.0)) == []


def test_m1_fractional_uses_market_1555():
    m = M1VolTrend(fractional=True)
    out = m.on_event(close_event(MON), snap(last_close=500.0, vol_d_21=0.10, vol_d_63=0.20, ret_12m_ex_1m=0.05), PositionView(0.0))
    assert out[0].entry_style == "market_1555" and out[0].exit_style == "market_1555"


# ------------------------------------------------------------------------------------------------- M2
def bar_event(hh, mm, d=TUE):
    return ScheduleEvent("bar_close", datetime.combine(d, time(hh, mm), NY), d, "1m")


def test_m2_signal_threshold_sign_and_filters():
    m = M2IntradayMomentum(["SPY"])
    assert m.signal(snap(r1=0.004, sigma_last30=0.005)) == 1      # |r1| > 0.5 * sigma
    assert m.signal(snap(r1=-0.004, sigma_last30=0.005)) == -1
    assert m.signal(snap(r1=0.002, sigma_last30=0.005)) == 0
    assert m.signal(snap(r1=None, sigma_last30=0.005)) == 0 and m.signal(snap(r1=0.01, sigma_last30=None)) == 0
    a = M2IntradayMomentum(["SPY"], agreement=True)
    assert a.signal(snap(r1=0.004, r12=0.001, sigma_last30=0.005)) == 1
    assert a.signal(snap(r1=0.004, r12=-0.001, sigma_last30=0.005)) == 0 and a.signal(snap(r1=0.004, r12=None, sigma_last30=0.005)) == 0
    rv = M2IntradayMomentum(["SPY"], relvol_filter=True)
    assert rv.signal(snap(r1=0.004, sigma_last30=0.005, relvol_1515=1.2)) == 1
    assert rv.signal(snap(r1=0.004, sigma_last30=0.005, relvol_1515=0.8)) == 0 and rv.signal(snap(r1=0.004, sigma_last30=0.005)) == 0
    k1 = M2IntradayMomentum(["SPY"], k=1.0)
    assert k1.signal(snap(r1=0.004, sigma_last30=0.005)) == 0


def test_m2_trades_once_at_entry_time_and_exits_at_the_right_kind():
    m = M2IntradayMomentum(["SPY"])
    s = snap(last_close=500.0, r1=0.004, sigma_last30=0.005)
    assert m.on_event(bar_event(15, 28), s, PositionView(0.0)) == [], "not the entry minute"
    out = m.on_event(bar_event(15, 29), s, PositionView(0.0))          # bar 15:29-15:30 -> decision at 15:30
    assert len(out) == 1 and out[0].direction == 1 and out[0].entry_style == "marketable_limit" and out[0].exit_style == "cls"
    assert out[0].horizon_seconds == 1800 and out[0].max_holding_seconds == 1800 and not out[0].overnight_ok and out[0].protective_stop_price is None
    assert out[0].risk_budget_pct == 0.0025 and out[0].volatility == 0.005
    assert m.on_event(bar_event(15, 29), s, PositionView(0.0)) == [], "one trade per session"
    assert m.on_event(ScheduleEvent("t1550", datetime.combine(TUE, time(15, 49), NY), TUE), s, PositionView(20.0, 500.0)) [0].direction == 0
    assert m.on_event(ScheduleEvent("t1558", datetime.combine(TUE, time(15, 57), NY), TUE), s, PositionView(20.0, 500.0)) == [], "whole-share exit is at t1550"
    f = M2IntradayMomentum(["SPY"], fractional=True, entry_time="15:15")
    assert f.exit_kind == "t1558" and "t1558" in f.listens
    o = f.on_event(bar_event(15, 14), s, PositionView(0.0))
    assert len(o) == 1 and o[0].exit_style == "market_1558"
    ex = f.on_event(ScheduleEvent("t1558", datetime.combine(TUE, time(15, 57), NY), TUE), s, PositionView(1.5, 500.0))
    assert ex[0].direction == 0 and ex[0].exit_style == "market_1558"


def test_m2_fat_tail_guard_exits_at_market_once():
    m = M2IntradayMomentum(["SPY"])
    s = snap(last_close=500.0, r1=0.004, sigma_last30=0.005)
    m.on_event(bar_event(15, 29), s, PositionView(0.0))
    pos = PositionView(20.0, 500.0)
    assert m.on_event(bar_event(15, 35), snap(last_close=495.0, sigma_last30=0.005), pos) == [], "-1% is inside 3 sigma (1.5%)"
    out = m.on_event(bar_event(15, 36), snap(last_close=490.0, sigma_last30=0.005), pos)
    assert len(out) == 1 and out[0].direction == 0 and out[0].exit_style == "market" and "fat tail" in out[0].tag
    assert m.on_event(bar_event(15, 37), snap(last_close=489.0, sigma_last30=0.005), pos) == [], "exit already sent this session"
    short = M2IntradayMomentum(["SPY"])
    short.on_event(bar_event(15, 29), snap(last_close=500.0, r1=-0.004, sigma_last30=0.005), PositionView(0.0))
    assert short.on_event(bar_event(15, 36), snap(last_close=510.0, sigma_last30=0.005), PositionView(-20.0, 500.0))[0].direction == 0


# ------------------------------------------------------------------------------------------------- M3
def m3_snaps(**over):
    base = dict(last_close=100.0, res5=-0.05, sigma_res_21d=0.02)
    out = {}
    for i, s in enumerate(["A", "B", "C", "D", "E"]):
        kw = dict(base)
        kw["res5"] = -0.05 + 0.015 * i         # A -0.05, B -0.035, C -0.02, D -0.005, E +0.01
        kw.update(over.get(s, {}))
        out[s] = snap(s, **kw)
    return out


def test_m3_ranks_bottom_n_among_eligible_names():
    m = M3ResidualReversal(["A", "B", "C", "D", "E"])
    pos = {s: PositionView(0.0) for s in "ABCDE"}
    out = m.on_event_batch(close_event(TUE), m3_snaps(), pos)
    assert [i.symbol for i in out] == ["A", "B", "C"] and all(i.direction == 1 and i.entry_style == "opg" and i.exit_style == "cls" for i in out)
    it = out[0]
    assert it.volatility == pytest.approx(0.02 * 5 ** 0.5) and it.risk_budget_pct == 0.0025 and it.overnight_ok and it.horizon_seconds == 5 * 86400
    assert it.protective_stop_price == pytest.approx(100.0 * (1 - 3 * 0.02 * 5 ** 0.5))
    # ineligible: wide spread, earnings, synthetic, missing residual
    m2 = M3ResidualReversal(["A", "B", "C", "D", "E"], n_bottom=2)
    snaps = m3_snaps(A={"spread_bps": 8.0}, B={"earnings_within_5": True}, C={"synthetic_frac_30m": 0.5})
    out = m2.on_event_batch(close_event(TUE), snaps, pos)
    assert [i.symbol for i in out] == ["D"], "E has positive residual and is never bought"
    fr = M3ResidualReversal(["A"], fractional=True)
    assert fr.on_event_batch(close_event(TUE), m3_snaps(), pos)[0].entry_style == "limit_at_prev_close_cancel_0945"


def test_m3_exits_on_positive_residual_hold_limit_and_fat_tail():
    m = M3ResidualReversal(["A", "B", "C"], hold_sessions=2)
    flat = {s: PositionView(0.0) for s in "ABC"}
    m.on_event_batch(close_event(date(2026, 9, 14)), m3_snaps(), flat)   # enters A, B, C
    held = {s: PositionView(10.0, 100.0) for s in "ABC"}
    d1 = date(2026, 9, 15)
    out = m.on_event_batch(close_event(d1), m3_snaps(A={"res5": 0.01}, C={"last_close": 80.0}), held)
    exits = {i.symbol: i.tag for i in out if i.direction == 0}
    assert exits == {"A": "res5>0", "C": "fat tail"} and all(i.exit_style == "cls" for i in out if i.direction == 0)
    assert [i.symbol for i in out if i.direction == 1] == ["D"], "freed slots are refilled from the ranking in the same event"
    out = m.on_event_batch(close_event(date(2026, 9, 16)), m3_snaps(), {"B": PositionView(10.0, 100.0), "A": PositionView(0.0), "C": PositionView(0.0)})
    assert [(i.symbol, i.tag) for i in out if i.direction == 0] == [("B", "2 sessions")]
    assert not any(i.symbol == "B" and i.direction == 1 for i in out), "never re-enter a name in the same event it exits"
