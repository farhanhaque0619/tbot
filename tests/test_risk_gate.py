"""Phase 13: deterministic pre-trade gate. Every check has a test that makes it fail alone."""
import math
from datetime import datetime

import pytest

from bot.execution.broker import AccountInfo, AssetInfo, OrderInfo
from bot.execution.market_state import MarketState
from bot.risk import OrderIntent, RiskLimits, RiskManager, SafeLiveLimits
from bot.risk.sizing import fixed_fractional_qty, round_qty

ASSET = AssetInfo("SPY", tradable=True, fractionable=True, shortable=True, marginable=True, easy_to_borrow=True)
NOFRAC = AssetInfo("BRK.A", tradable=True, fractionable=False, shortable=False, marginable=False, easy_to_borrow=False)


def acct(**kw) -> AccountInfo:
    base = dict(equity=100.0, cash=100.0, buying_power=200.0, account_number="PA1", status="ACTIVE", shorting_enabled=True)
    base.update(kw)
    return AccountInfo(**base)


def state(**kw) -> MarketState:
    base = dict(timestamp="t", symbol="SPY", market_open=True, last_price=500.0, bid=499.95, ask=500.05, mid=500.0, spread_bps=2.0,
                bar_return_1=0.0, bar_return_n=0.0, atr=5.0, realized_vol=0.15, fast_ma=1, slow_ma=1, distance_from_ma=0, volume=1,
                volume_zscore=0, position_qty=0.0, position_notional=0.0, cash=100.0, equity=100.0, buying_power=200.0,
                gross_exposure=0.0, net_exposure=0.0, daily_pnl=0.0, drawdown=0.0, open_orders=0, stale_data_seconds=5.0,
                last_bar_date="2024-01-05", n_bars=300)
    base.update(kw)
    return MarketState(**base)


def intent(**kw) -> OrderIntent:
    base = dict(symbol="SPY", side="buy", qty=0.04, kind="entry", reference_price=500.0, client_order_id="cid-1", tif="day")
    base.update(kw)
    return OrderIntent(**base)


def rm(safe=True, **limits) -> RiskManager:
    L = RiskLimits(allow_fractional=True, max_positions=2, **limits)
    m = RiskManager(L, safe=SafeLiveLimits(max_order_notional=25, max_gross_exposure=50, max_daily_loss=5, max_account_drawdown=10, max_positions=1, allowed_symbols=("SPY",)) if safe else None)
    m.update_equity(datetime(2024, 1, 8), 100.0)
    return m


def check(m=None, i=None, s=None, a=None, **kw):
    m = m or rm()
    base = dict(state=s or state(), account=a or acct(), asset=ASSET, broker_env="paper", expected_env="paper",
                account_is_paper_shaped=True, open_orders=[], known_client_ids=[], position_qty=0.0, n_positions=0, tif="day", bar_current=True)
    base.update(kw)
    return m.check_order(i or intent(), **base)


def test_approves_clean_entry():
    d = check()
    assert d.approved and d.code == "APPROVED" and not d.failed


@pytest.mark.parametrize("kw, code", [
    (dict(a=acct(trading_blocked=True)), "account_healthy"),
    (dict(a=acct(status="INACTIVE")), "account_healthy"),
    (dict(broker_env="live"), "mode_consistent"),
    (dict(account_is_paper_shaped=False), "mode_consistent"),
    (dict(known_client_ids=["cid-1"]), "no_duplicate_order"),
    (dict(open_orders=[OrderInfo("1", "x", "SPY", "buy", 1, "new", 0, None, None, None)]), "no_outstanding_order_conflict"),
    (dict(i=intent(qty=0)), "qty_positive"),
    (dict(s=state(mid=600.0)), "price_sane"),
    (dict(asset=NOFRAC), "fractionable_if_needed"),
    (dict(tif="opg"), "fractional_tif_day"),
    (dict(asset=AssetInfo("X", False, True, True, True, True)), "symbol_tradable"),
    (dict(position_qty=0.1), "strategy_state_valid"),
    (dict(s=state(stale_data_seconds=5000)), "data_fresh"),
    (dict(bar_current=False), "data_fresh"),
    (dict(s=state(market_open=False)), "market_permitted"),
    (dict(s=state(spread_bps=80)), "spread_sane"),
    (dict(n_positions=1), "position_limit"),
    (dict(i=intent(qty=0.5)), "order_notional"),           # $250 > 50% of $100 equity
    (dict(a=acct(cash=10.0)), "buying_power"),
    (dict(i=intent(side="sell")), "no_short_unless_allowed"),
    (dict(i=intent(symbol="QQQ")), "safe_symbol_allowed"),
    (dict(i=intent(qty=0.06)), "safe_order_notional"),      # $30 > $25
    (dict(s=state(gross_exposure=40.0)), "safe_gross_exposure"),
    (dict(s=state(daily_pnl=-6.0)), "safe_daily_loss"),
])
def test_each_entry_check_can_fail_alone(kw, code):
    d = check(**kw)
    assert not d.approved and d.code == code, d.to_dict()


def test_kill_switch_and_daily_halt_block_entries_but_not_exits():
    m = rm()
    m.update_equity(datetime(2024, 1, 8, 10), 100.0)
    m.update_equity(datetime(2024, 1, 8, 11), 80.0)      # -20% -> killed AND daily halt
    assert m.killed and m.halted_today
    d = check(m=m)
    assert not d.approved and d.code == "kill_switch_clear"
    ex = check(m=m, i=intent(side="sell", qty=0.04, kind="exit"), position_qty=0.04)
    assert ex.approved, ex.to_dict()


def test_exit_validation():
    d = check(i=intent(side="sell", qty=0.05, kind="exit"), position_qty=0.04)
    assert d.code == "strategy_state_valid"          # exit more than we hold
    d = check(i=intent(side="buy", qty=0.04, kind="exit"), position_qty=0.04)
    assert d.code == "strategy_state_valid"          # wrong side for a long
    d = check(i=intent(side="sell", qty=0.04, kind="exit"), position_qty=0.04, known_client_ids=["cid-1"])
    assert d.code == "no_duplicate_order"            # integrity checks still apply to exits


def test_safe_drawdown_dollar_cap():
    m = rm()
    m.update_equity(datetime(2024, 1, 8, 10), 100.0)
    for i, eq in enumerate((98.0, 96.0, 94.0, 92.0), start=9):   # -$2/day: under the $5 and 3% daily caps
        m.update_equity(datetime(2024, 1, i, 10), eq)              # peak 100, now 92: under the $10 drawdown cap, under 20% kill
    assert not m.killed and not m.halted_today
    d = check(m=m, a=acct(equity=92.0, cash=92.0), s=state(equity=92.0, cash=92.0))
    assert d.approved, d.to_dict()
    # the gate sees the live account equity BEFORE the next equity mark is fed to the manager
    d = check(m=m, a=acct(equity=89.0, cash=89.0), s=state(equity=89.0, cash=89.0))
    assert d.code == "safe_drawdown"


def test_opg_orders_are_fresh_on_completed_bar_not_quote():
    d = check(i=intent(qty=1.0), tif="opg", s=state(stale_data_seconds=20000, market_open=False),
              a=acct(equity=1000, cash=1000, buying_power=1000), m=rm(safe=False))
    assert d.approved, d.to_dict()


def test_without_safe_mode_margin_and_shorts_follow_account():
    m = rm(safe=False)
    d = check(m=m, i=intent(side="sell"), a=acct(shorting_enabled=True))
    assert d.approved
    d = check(m=m, i=intent(side="sell"), a=acct(shorting_enabled=False))
    assert d.code == "no_short_unless_allowed"


def test_fractional_rounding_and_sizing():
    assert round_qty(0.123456, fractional=True, decimals=3) == 0.123
    assert round_qty(0.9999, fractional=False) == 0.0
    assert round_qty(2.9999, fractional=False) == 2.0
    assert round_qty(float("nan"), fractional=True) == 0.0
    # $100 equity, 1% risk, $10 stop -> 0.1 sh ($50) -> capped by $25 notional -> 0.05
    assert fixed_fractional_qty(100, 500, 10, risk_pct=0.01, max_position_pct=0.5, cash_available=100, fractional=True, max_notional=25) == 0.05
    assert fixed_fractional_qty(100, 500, 10, risk_pct=0.01, max_position_pct=0.5, fractional=False) == 0.0
    assert fixed_fractional_qty(100_000, 50, 2.0, risk_pct=0.01, max_position_pct=0.5) == 500
