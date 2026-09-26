from datetime import date

import pytest

from bot.core.policy import RiskPolicy, legacy_policy
from bot.execution.broker import AccountInfo
from bot.risk.policy_engine import ExposureLedger, RiskEngine, limits_from_policy
from tests.v15.helpers import intent

PAPER = RiskPolicy.load("config/policy.paper.yaml")
LIVE = RiskPolicy.load("config/policy.live.yaml")
ACCT = AccountInfo(100.0, 100.0, 100.0, account_number="PA1", status="ACTIVE", shorting_enabled=True)


def test_policy_is_frozen_fingerprinted_and_round_trips(tmp_path):
    with pytest.raises(Exception):
        PAPER.max_gross_pct = 5.0   # type: ignore[misc]
    assert PAPER.fingerprint() != LIVE.fingerprint() and len(PAPER.fingerprint()) == 64
    p = PAPER.save(tmp_path / "p.yaml")
    assert RiskPolicy.load(p).fingerprint() == PAPER.fingerprint()
    assert "M2" not in LIVE.allowed_modules and not LIVE.allow_short and not LIVE.allow_margin and LIVE.overnight_allowed("M1")
    assert legacy_policy().max_sector_pct_of_gross == 1.0


def test_limits_from_policy_maps_fields():
    L = limits_from_policy(LIVE)
    assert L.max_drawdown_pct == 0.12 and L.daily_loss_limit_pct == 0.02 and L.max_positions == 6 and L.risk_per_trade_pct == 0.01


def test_admit_checks():
    r = RiskEngine(LIVE)
    ok = r.admit(intent("SPY", "M1", overnight=True), spread_bps=1.0, stale_seconds=10, is_etf=True, account=ACCT)
    assert ok.approved
    assert r.admit(intent("SPY", "M2"), spread_bps=1.0, stale_seconds=10, is_etf=True, account=ACCT).code == "module_allowed"
    assert r.admit(intent("IWM", "M1"), spread_bps=1.0, stale_seconds=10, is_etf=True, account=ACCT).code == "symbol_allowed"
    assert r.admit(intent("SPY", "M1", direction=-1), spread_bps=1.0, stale_seconds=10, is_etf=True, account=ACCT).code == "short_allowed"
    assert r.admit(intent("SPY", "M1"), spread_bps=9.0, stale_seconds=10, is_etf=True, account=ACCT).code == "spread_ok"
    assert r.admit(intent("SPY", "M1"), spread_bps=1.0, stale_seconds=5000, is_etf=True, account=ACCT).code == "data_fresh"
    r.halt_entries, r.halt_reason = True, "watchdog"
    assert r.admit(intent("SPY", "M1"), spread_bps=1.0, stale_seconds=1, is_etf=True, account=ACCT).code == "entries_not_halted"
    assert r.admit(intent("SPY", "M1", direction=0), spread_bps=None, stale_seconds=None, is_etf=True).approved, "exits always admitted"


def test_pdt_legacy_guard_blocks_fourth_day_trade():
    pol = PAPER.model_copy(update={"pdt_mode": "legacy_guard"})
    r = RiskEngine(pol)
    sessions = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
    for d in sessions[:3]:
        r.ledger.apply_fill("M2", "SPY", 1, 100.0, d)
        r.ledger.apply_fill("M2", "SPY", -1, 101.0, d)      # opened and closed the same session = a day trade
    assert r.ledger.day_trades_in_window(sessions) == 3
    d = r.admit(intent("SPY", "M2"), spread_bps=1.0, stale_seconds=1, is_etf=True, account=ACCT, recent_sessions=sessions)
    assert d.code == "pdt_legacy_guard"
    r2 = RiskEngine(PAPER)   # paper policy: intraday_margin -> no guard
    assert r2.admit(intent("SPY", "M2"), spread_bps=1.0, stale_seconds=1, is_etf=True, account=ACCT, recent_sessions=sessions).approved
    # the account's own counter also blocks
    acct = AccountInfo(100.0, 100.0, 100.0, account_number="PA1", status="ACTIVE", daytrade_count=3)
    assert r.admit(intent("SPY", "M2"), spread_bps=1.0, stale_seconds=1, is_etf=True, account=acct, recent_sessions=[]).code == "pdt_legacy_guard"


def test_ledger_exposures():
    L = ExposureLedger(sectors={"XLK": "tech"})
    L.apply_fill("A", "XLK", 10, 100.0, date(2026, 9, 25))
    L.apply_fill("B", "SPY", -5, 200.0, date(2026, 9, 25))
    L.inflight["c"] = ("A", "QQQ", 500.0)
    px = {"XLK": 110.0, "SPY": 210.0}
    assert L.gross(px) == pytest.approx(1100 + 1050 + 500) and L.net(px) == pytest.approx(1100 - 1050 + 500)
    assert L.module_gross("A", px) == pytest.approx(1600) and L.sector_gross("tech", px) == pytest.approx(1100) and L.open_positions() == 2
    assert L.symbol_qty("XLK") == 10


def test_throttle_only_tightens_and_operator_restores():
    r = RiskEngine(PAPER)
    for _ in range(60):
        assert r.record_trade_pnl("M2", -1.0) in (None, "M2")
    assert r.budget_multiplier("M2") == 0.5
    for _ in range(60):
        r.record_trade_pnl("M2", +5.0)
    assert r.budget_multiplier("M2") == 0.5, "winning does not automatically restore the budget"
    r.unthrottle("M2")
    assert r.budget_multiplier("M2") == 1.0
    assert RiskEngine(PAPER, throttle_enabled=False).record_trade_pnl("M2", -1.0) is None
