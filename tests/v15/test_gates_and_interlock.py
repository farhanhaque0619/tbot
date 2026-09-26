"""Paper/live gates from the store and the policy-fingerprint interlock."""
from datetime import date, datetime, timedelta, timezone

import pytest

from bot.config import Settings
from bot.core.policy import RiskPolicy
from bot.execution.gates import GateResult, live_gates, paper_gates, summary
from bot.execution.interlock import LiveInterlock
from bot.execution.store import ExecutionStore

LIVE = RiskPolicy.load("config/policy.live.yaml")
PAPER = RiskPolicy.load("config/policy.paper.yaml")


def test_paper_gates_unknown_without_data_and_promotion_gate_fails(tmp_path):
    st = ExecutionStore(tmp_path / "x.sqlite")
    g = paper_gates(st, LIVE, promotions_path=tmp_path / "none.md")
    by = {x.name: x for x in g}
    assert by["sessions_autonomous_ge_60"].ok is None and by["slippage_within_3bps_of_backtest"].ok is None
    assert by["promotion_record_for_live_modules"].ok is False and "M1" in by["promotion_record_for_live_modules"].detail
    s = summary(g)
    assert s["all_pass"] is False and s["unknown"] >= 3 and GateResult("x", None, "").label == "UNKNOWN"


def test_paper_gates_with_populated_store(tmp_path):
    st = ExecutionStore(tmp_path / "x.sqlite")
    base = datetime(2026, 1, 5, 16, tzinfo=timezone.utc)
    for i in range(61):
        st.heartbeat("daemon_session", base + timedelta(days=i), "ok")
    for a in ("alert", "halt_entries", "cancel_pending_entries", "flatten"):
        st.heartbeat(f"watchdog:{a}", base, "exercised")
    from bot.execution.oms import OrderRecord
    rec = OrderRecord("c1", "M2", "SPY", "buy", 10, "entry", "marketable_limit", date(2026, 1, 5), base, 100.0, broker_id="b1", status="filled", filled_qty=10, avg_price=100.03)
    st.update_order(rec)
    from bot.core.events import TradeUpdateEvent
    st.record_fill(TradeUpdateEvent("b1", "c1", "fill", base, "SPY", "buy", 10, 10, 100.03, "filled"))
    for i in range(120):
        st.add_trade({"module": "M2", "symbol": "SPY", "qty": 1, "entry_price": 100, "exit_price": 101, "pnl": 1.0 if i % 3 else -0.5, "exit_reason": "exit", "exit_ts": base, "session": date(2026, 1, 5)})
    prom = tmp_path / "PROMOTIONS.md"
    prom.write_text("## M1 promoted 2026-01-01 test\n")
    g = {x.name: x for x in paper_gates(st, LIVE, backtest_slippage_bps=3.0, backtest_m2_expectancy_sign=1, promotions_path=prom)}
    assert g["sessions_autonomous_ge_60"].ok and g["zero_discrepancies_orphans_duplicates"].ok and g["watchdog_actions_exercised"].ok
    assert g["slippage_within_3bps_of_backtest"].ok and "+3.0 bps" in g["slippage_within_3bps_of_backtest"].detail
    assert g["m2_120_trades_same_sign_expectancy"].ok and g["promotion_record_for_live_modules"].ok
    assert summary(list(g.values()))["all_pass"]
    lg = {x.name: x for x in live_gates(st, safe_mode_sessions=0, last_stepup=None, paper_slippage_bps=1.0)}
    assert lg["stepup_at_most_2x_per_month"].ok and lg["slippage_degradation_lt_5bps_vs_paper"].ok


def test_interlock_policy_fingerprint(tmp_path, monkeypatch):
    s = Settings(alpaca_live_api_key="AKTESTTESTTESTTEST", alpaca_live_secret_key="y" * 40, trading_env="live", state_dir=tmp_path,
                 alpaca_paper_api_key="PKTESTTESTTESTTEST", alpaca_paper_secret_key="x" * 40)
    il = LiveInterlock(s)
    from bot.config import LIVE_CONFIRMATION_PHRASE
    st = il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True, policy_fingerprint=LIVE.fingerprint())
    assert st.policy_fingerprint == LIVE.fingerprint()
    assert il.is_armed()[0] and il.is_armed(policy_fingerprint=LIVE.fingerprint())[0]
    ok, why = il.is_armed(policy_fingerprint=PAPER.fingerprint())
    assert not ok and "policy changed" in why
    gates = {g.name: g for g in il.check(cli_live_flag=True, policy=PAPER, promotions_path=tmp_path / "none.md")}
    assert gates["policy_fingerprint_matches"].ok is False and gates["live_modules_promoted"].ok is False
    gates = {g.name: g for g in il.check(cli_live_flag=True, policy=LIVE, promotions_path=tmp_path / "none.md")}
    assert gates["policy_fingerprint_matches"].ok is True and gates["live_modules_promoted"].ok is False, "M1 is not promoted"
    # an arm file without a fingerprint (pre-V1.5) is not enough once a policy is in play
    il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True)
    assert il.is_armed()[0] and not il.is_armed(policy_fingerprint=LIVE.fingerprint())[0]
    with pytest.raises(PermissionError):
        il.arm(typed_phrase="nope", acknowledged=True)
