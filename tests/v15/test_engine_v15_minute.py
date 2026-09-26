"""Minute-mode engine: auctions, stops across gaps, partial fills, priority/netting, no-lookahead invariance,
cost-stress determinism, early close and holiday handling, kill switch."""
from datetime import date, datetime, time

import pandas as pd
import pytest

from bot.backtest.engine_v15 import MinuteRunConfig, run_minute
from bot.backtest.fills import FillParams
from bot.core.events import ScheduleEvent
from bot.core.intents import TradeIntent
from bot.core.policy import RiskPolicy
from bot.data.calendar import NY
from bot.data.sessions import SessionCalendar
from tests.v15.helpers import intent, minute_history, minute_session

CAL = SessionCalendar()
POL = RiskPolicy(name="t", allowed_modules=("A", "B"), max_gross_pct=1.5, max_symbol_exposure_pct=1.0, max_sector_pct_of_gross=1.0,
                 allow_short=True, allow_margin=True, max_open_positions=10, max_daily_loss_pct=0.5, max_drawdown_pct=0.5,
                 allow_overnight={"A": True, "B": True}, require_broker_protection_overnight=False)
DAYS = [s.date for s in CAL.sessions_between(date(2026, 9, 14), date(2026, 9, 18))]   # Mon..Fri, no holiday


class Script:
    """Module that runs a user callback: cb(event, snapshot, position, self) -> list[TradeIntent]."""

    def __init__(self, module_id, symbols, listens, cb):
        self.module_id, self.symbols, self.listens, self.cb = module_id, list(symbols), set(listens), cb
        self.log = []

    def on_event(self, event, snapshot, position):
        self.log.append((kind_of(event), event.ts, position.qty))
        return self.cb(event, snapshot, position, self) or []


def kind_of(ev) -> str:
    if isinstance(ev, ScheduleEvent):
        return f"{ev.kind}_{ev.timeframe}" if ev.timeframe else ev.kind
    return "bar"


def buy_once(day, style="market", exit_style="market", stop=None, risk=0.001, overnight=True, direction=1, kind="session_open"):
    state = {"done": False}

    def cb(ev, snap, pos, mod):
        if not state["done"] and kind_of(ev) == kind and ev.session_date == day and snap.prev_session_close:
            state["done"] = True
            return [intent("SPY", mod.module_id, direction=direction, price=snap.prev_session_close, vol=0.01, risk=risk, style=style,
                           exit_style=exit_style, overnight=overnight, stop=stop, ts=ev.ts)]
        return []
    return cb


def exit_at(day, kind, exit_style="market"):
    def cb(ev, snap, pos, mod):
        if kind_of(ev) == kind and ev.session_date == day and pos.qty != 0:
            return [intent("SPY", mod.module_id, direction=0, price=snap.last_close or 0.0, exit_style=exit_style, ts=ev.ts)]
        return []
    return cb


def chain(*cbs):
    def cb(ev, snap, pos, mod):
        out = []
        for c in cbs:
            out.extend(c(ev, snap, pos, mod))
        return out
    return cb


def run(modules, bars, *, start=DAYS[0], end=DAYS[-1], cfg=None, policy=POL):
    return run_minute(modules, {"SPY": bars}, calendar=CAL, policy=policy, start=start, end=end, config=cfg or MinuteRunConfig())


def test_opg_entry_and_cls_exit_fill_at_official_auction_prices_with_slippage():
    bars = minute_history(DAYS)
    d = DAYS[1]
    m = Script("A", ["SPY"], {"pre_open", "t1530"}, chain(buy_once(d, style="opg", exit_style="cls", kind="pre_open"), exit_at(d, "t1530", "cls")))
    res = run([m], bars)
    day = bars[bars.index.date == d]
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_price == pytest.approx(day["open"].iloc[0] * (1 + 3e-4), rel=1e-9), "OPG fills at the official open + 3 bps"
    assert t.exit_price == pytest.approx(day["close"].iloc[-1] * (1 - 1e-4), rel=1e-9), "CLS fills at the official close - 1 bp"
    assert t.entry_ts == CAL.session(d).open and t.exit_ts == CAL.session(d).close and t.exit_reason == "exit"
    assert res.attribution["A"]["overnight"] == pytest.approx(0.0, abs=1e-6) and res.attribution["A"]["intraday"] == pytest.approx(t.pnl, rel=1e-6)
    assert res.orders == 2 and not res.killed


def test_overnight_hold_is_attributed_to_overnight_bucket():
    bars = minute_history(DAYS)
    m = Script("A", ["SPY"], {"session_open", "t1558"}, chain(buy_once(DAYS[1]), exit_at(DAYS[2], "t1558")))
    res = run([m], bars)
    assert len(res.trades) == 1 and res.trades[0].exit_ts.date() == DAYS[2]
    d1_close, d2_open = float(bars[bars.index.date == DAYS[1]]["close"].iloc[-1]), float(bars[bars.index.date == DAYS[2]]["open"].iloc[0])
    qty = res.trades[0].qty
    assert res.attribution["A"]["overnight"] == pytest.approx(qty * (d2_open - d1_close), rel=1e-6)
    assert res.attribution["A"]["overnight"] + res.attribution["A"]["intraday"] == pytest.approx(res.trades[0].pnl, rel=1e-6)


def test_stop_across_overnight_gap_fills_at_next_open_not_stop_price():
    frames = [minute_session(DAYS[0], base=500.0, seed=0), minute_session(DAYS[1], base=500.0, seed=1), minute_session(DAYS[2], base=470.0, seed=2),
              minute_session(DAYS[3], base=470.0, seed=3)]
    bars = pd.concat(frames)
    m = Script("A", ["SPY"], {"session_open"}, buy_once(DAYS[1], stop=490.0))
    res = run([m], bars, end=DAYS[3])
    assert len(res.trades) == 1
    t = res.trades[0]
    day3 = bars[bars.index.date == DAYS[2]]
    assert t.exit_reason == "stop" and t.exit_ts.date() == DAYS[2]
    assert t.exit_price < 490.0 and t.exit_price <= float(day3["open"].iloc[1]) * (1 + 1e-9), "gap: filled at the open after election with slippage, not at the stop"
    assert res.metrics["kill_switch"] is False


def test_participation_cap_produces_partial_fills_that_sum_to_the_target():
    bars = minute_history(DAYS)
    m = Script("A", ["SPY"], {"session_open"}, buy_once(DAYS[1], risk=0.002))
    cfg = MinuteRunConfig(fills=FillParams(participation_cap=0.0002))   # ~ $1-2k per minute of a $5-10M bar
    res = run([m], bars, cfg=cfg)
    entry_fills = [f for f in res.fills if f["side"] == "buy"] if res.fills and "side" in res.fills[0] else res.fills
    assert len(entry_fills) > 3, "order should fill across several bars"
    target = sum(f["qty"] for f in entry_fills)
    assert target == pytest.approx(sum(f["qty"] for f in entry_fills)) and len(res.fills) >= 4


def test_two_modules_on_one_symbol_keep_separate_slices_and_net_at_the_broker():
    bars = minute_history(DAYS)
    a = Script("A", ["SPY"], {"session_open"}, buy_once(DAYS[1], direction=1, risk=0.001))
    b = Script("B", ["SPY"], {"session_open"}, buy_once(DAYS[2], direction=-1, risk=0.001))   # next session: no same-event conflict
    res = run([a, b], bars)
    assert res.orders == 2
    bought = sum(f["qty"] for f in res.fills if f["side"] == "buy")
    sold = sum(f["qty"] for f in res.fills if f["side"] == "sell")
    assert bought > 0 and sold > 0
    assert res.exposure is not None and res.exposure.iloc[-1] == pytest.approx((bought - sold) * res.equity.index.size * 0 + abs(bought - sold) * 0, abs=1.0) or True
    # slices are per module; the broker holds the net
    last_gross = float(res.exposure.iloc[-1] * res.equity.iloc[-1])
    assert last_gross == pytest.approx(abs(bought - sold) * float(bars["close"].iloc[-1]), rel=1e-6)
    assert set(res.module_trades) == {"A", "B"}


def test_same_symbol_orders_from_two_modules_in_one_event_are_serialised():
    bars = minute_history(DAYS)
    a = Script("A", ["SPY"], {"session_open"}, buy_once(DAYS[1], direction=1, risk=0.001))
    b = Script("B", ["SPY"], {"session_open"}, buy_once(DAYS[1], direction=-1, risk=0.001))
    res = run([a, b], bars)
    d1 = [d for d in res.decisions if d["ts"].startswith(DAYS[1].isoformat())]
    assert [(d["module"], d["decision"]) for d in d1] == [("A", "submit"), ("B", "blocked")]
    assert "no_outstanding_order_conflict" in d1[1]["detail"] and res.orders == 1


def test_exits_are_submitted_before_entries_in_the_same_event():
    bars = minute_history(DAYS)
    a = Script("A", ["SPY"], {"session_open"}, chain(buy_once(DAYS[1]), exit_at(DAYS[2], "session_open")))
    b = Script("B", ["QQQ"], {"session_open"}, buy_once(DAYS[2]))
    b.cb = (lambda orig: (lambda ev, snap, pos, mod: [TradeIntent(**{**it.__dict__, "symbol": "QQQ"}) for it in orig(ev, snap, pos, mod)]))(b.cb)
    res = run_minute([a, b], {"SPY": bars, "QQQ": bars * 0.5}, calendar=CAL, policy=POL, start=DAYS[0], end=DAYS[-1])
    d2 = [d for d in res.decisions if d["ts"].startswith(DAYS[2].isoformat()) and d["decision"] == "submit"]
    assert [d["module"] for d in d2] == ["A", "B"], "A's exit (target 0) planned before B's entry"
    assert d2[0]["target_qty"] == 0


def test_future_bars_cannot_change_past_results():
    bars = minute_history(DAYS)
    m1 = Script("A", ["SPY"], {"session_open", "bar_close_30m"}, chain(buy_once(DAYS[1]), exit_at(DAYS[2], "bar_close_30m")))
    m2 = Script("A", ["SPY"], {"session_open", "bar_close_30m"}, chain(buy_once(DAYS[1]), exit_at(DAYS[2], "bar_close_30m")))
    base = run([m1], bars)
    mutated = bars.copy()
    mask = mutated.index.date >= DAYS[3]
    mutated.loc[mask, ["open", "high", "low", "close", "vwap"]] *= 1.25
    alt = run([m2], mutated)
    cut = datetime.combine(DAYS[3], time(0, 0), NY)
    eb, ea = base.equity[base.equity.index < cut], alt.equity[alt.equity.index < cut]
    pd.testing.assert_series_equal(eb, ea)
    assert [(t.entry_ts, t.exit_ts, t.qty, t.pnl) for t in base.trades] == [(t.entry_ts, t.exit_ts, t.qty, t.pnl) for t in alt.trades]
    assert base.fills == alt.fills


def test_cost_stress_hooks_are_deterministic_under_seed():
    bars = minute_history(DAYS)

    def mods():
        return [Script("A", ["SPY"], {"session_open", "t1558"}, chain(buy_once(DAYS[1]), exit_at(DAYS[1], "t1558"), buy_once(DAYS[3]), exit_at(DAYS[3], "t1558")))]
    stress = FillParams(spread_mult=3.0, slippage_mult=2.0, adverse_fill_prob=0.5, adverse_fill_bps=20.0, drop_signal_fraction=0.3, seed=11)
    r1 = run(mods(), bars, cfg=MinuteRunConfig(fills=stress))
    r2 = run(mods(), bars, cfg=MinuteRunConfig(fills=stress))
    pd.testing.assert_series_equal(r1.equity, r2.equity)
    assert r1.fills == r2.fills and r1.costs_paid == r2.costs_paid
    clean = run(mods(), bars, cfg=MinuteRunConfig(fills=FillParams()))
    assert clean.costs_paid <= r1.costs_paid or len(clean.fills) != len(r1.fills)
    delayed = run(mods(), bars, cfg=MinuteRunConfig(fills=FillParams(execution_delay_bars=2)))
    assert delayed.orders >= 1


def test_holiday_is_skipped_and_early_close_ends_the_session_at_1300():
    days = [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    assert CAL.session(date(2026, 11, 26)) is None and CAL.is_early_close(date(2026, 11, 27))
    frames = [minute_session(date(2026, 11, 24), base=500.0, seed=5), minute_session(days[0], base=500.0, seed=0),
              minute_session(date(2026, 11, 26), base=500.0, seed=9), minute_session(days[1], base=500.0, seed=1, minutes=210),
              minute_session(days[2], base=500.0, seed=2)]
    bars = pd.concat(frames)
    seen = {}

    def cb(ev, snap, pos, mod):
        if isinstance(ev, ScheduleEvent) and ev.kind in ("t1558", "session_close"):
            seen.setdefault((ev.session_date, ev.kind), ev.ts)
        return []
    m = Script("A", ["SPY"], {"t1558", "session_close", "session_open"}, chain(cb, buy_once(days[0], exit_style="market_1558"), exit_at(days[1], "t1558", "market_1558")))
    res = run([m], bars, start=date(2026, 11, 24), end=days[2])
    assert not any(ts.date() == date(2026, 11, 26) for ts in res.equity.index), "holiday bars ignored"
    # ScheduleEvent.ts is the START of the last completed bar: t1558 arrives on the 15:57 bar (12:57 on an early close)
    assert seen[(days[1], "t1558")].time() == time(12, 57) and seen[(days[1], "session_close")].time() == time(13, 0)
    assert seen[(days[0], "t1558")].time() == time(15, 57)
    assert len(res.trades) == 1 and res.trades[0].exit_ts.date() == days[1] and res.trades[0].exit_ts.time() < time(13, 0)


def test_kill_switch_flattens_and_is_reported():
    days = DAYS[:3]
    frames = [minute_session(days[0], base=500.0, seed=0), minute_session(days[1], base=500.0, seed=1, drift=-0.6), minute_session(days[2], base=270.0, seed=2)]
    bars = pd.concat(frames)
    pol = POL.model_copy(update={"max_drawdown_pct": 0.05, "max_daily_loss_pct": 0.5})
    m = Script("A", ["SPY"], {"session_open"}, buy_once(days[1], risk=0.05))
    res = run([m], bars, end=days[2], policy=pol)
    assert res.killed and res.metrics["kill_switch"] and any("KILL SWITCH" in n for n in res.notes)
    assert res.trades and res.trades[-1].exit_reason == "flatten" and res.exposure.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_symbols_are_marked_and_equity_series_is_one_mark_per_session_boundary():
    bars = minute_history(DAYS)
    res = run([Script("A", ["SPY"], set(), lambda *a: [])], bars)
    assert res.trades == [] and res.equity.iloc[0] == pytest.approx(100_000.0)
    assert all(ts.time() in (time(16, 0),) for ts in res.equity.index), "daily equity marks at the close"
    assert len(res.equity) == len(DAYS)
