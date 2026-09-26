"""Paper-only execution smoke test, exercised against the fake broker (open and closed market, every refusal)."""
from datetime import datetime, timedelta

import pytest

from bot.config import Settings
from bot.data.calendar import NY
from bot.execution import FakeBroker
from bot.execution.broker import AssetInfo
from bot.execution.smoke import FAIL, PASS, SKIP, PaperSmokeTest, SmokeRefusal

MON_10 = datetime(2026, 9, 28, 10, 0, tzinfo=NY)
SAT = datetime(2026, 9, 26, 14, 0, tzinfo=NY)


def make(tmp_path, *, now=MON_10, env="paper", cash=100_000.0, **kw):
    settings = Settings(_env_file=None, state_dir=tmp_path, log_dir=tmp_path, alpaca_paper_api_key="PKTESTX", alpaca_paper_secret_key="secret-x")
    b = FakeBroker(cash=cash, prices={"SPY": 771.30}, env=env, assets={"SPY": AssetInfo("SPY", True, True, True, True, True)})
    b.now = now
    b.quote_age = timedelta(seconds=2)
    return settings, b


def run(settings, b, tmp_path, **kw):
    # the injected sleep fills any open fake orders, standing in for the exchange
    t = PaperSmokeTest(settings=settings, broker=b, run_id=kw.pop("run_id", "smoke-test-1"), report_dir=tmp_path,
                       sleep=lambda s: b.fill_all(), poll_seconds=0, **kw)
    return t.run()


def test_full_cycle_when_market_open(tmp_path):
    settings, b = make(tmp_path)
    rep = run(settings, b, tmp_path)
    by = {s.name: s for s in rep.steps}
    assert rep.ok, [(s.name, s.detail) for s in rep.steps if s.status == FAIL]
    for name in ("env_is_paper", "account_active", "symbol_flat_no_open_orders", "asset_fractionable", "risk_gate", "entry_acknowledged",
                 "entry_read_by_client_id", "duplicate_client_id_rejected", "entry_filled", "broker_position_matches",
                 "state_reload_and_reconcile", "restart_no_duplicate", "exit_acknowledged", "exit_filled", "position_back_to_zero",
                 "no_smoke_orders_left_open"):
        assert by[name].status == PASS, (name, by[name].detail)
    cids = [o.client_order_id for o in b.submitted]
    assert cids == ["smoke-test-1-SPY-entry", "smoke-test-1-SPY-exit"], "exactly one entry and one exit, deterministic ids"
    assert b.positions == {} and not b.get_open_orders()
    entry = b.orders["smoke-test-1-SPY-entry"]
    assert entry.qty * 771.30 <= 2.05 and entry.time_in_force == "day" and entry.qty != int(entry.qty)
    assert (tmp_path / "smoke_paper_smoke-test-1.json").exists() and (tmp_path / "smoke-test-1.json").exists()
    assert all(s.request_id for s in rep.steps if s.order_id), "request ids logged for every order step"
    text = (tmp_path / "smoke_paper_smoke-test-1.json").read_text()
    assert "PKTESTX" not in text and "secret-x" not in text


def test_closed_market_ack_and_cancel_only(tmp_path):
    settings, b = make(tmp_path, now=SAT)
    rep = run(settings, b, tmp_path)
    by = {s.name: s for s in rep.steps}
    assert rep.ok and rep.market_open is False
    assert by["entry_acknowledged"].status == PASS and by["duplicate_client_id_rejected"].status == PASS
    assert by["entry_cancelled_market_closed"].status == PASS
    for name in ("entry_filled", "exit_filled", "position_back_to_zero"):
        assert by[name].status == SKIP
    assert b.positions == {} and not b.get_open_orders(), "nothing queued for Monday's open"
    assert b.orders["smoke-test-1-SPY-entry"].status == "canceled"


def test_refuses_non_paper_broker(tmp_path):
    settings, b = make(tmp_path, env="live")
    with pytest.raises(SmokeRefusal, match="paper-only"):
        PaperSmokeTest(settings=settings, broker=b, run_id="smoke-x")


def test_refuses_live_trading_env(tmp_path):
    settings, b = make(tmp_path)
    live = Settings(_env_file=None, state_dir=tmp_path, trading_env="live", alpaca_live_api_key="AKX", alpaca_live_secret_key="s")
    with pytest.raises(SmokeRefusal, match="TRADING_ENV"):
        PaperSmokeTest(settings=live, broker=b, run_id="smoke-x")


def test_refuses_large_notional_and_bad_run_id(tmp_path):
    settings, b = make(tmp_path)
    with pytest.raises(SmokeRefusal, match="notional"):
        PaperSmokeTest(settings=settings, broker=b, notional=100, run_id="smoke-x")
    with pytest.raises(SmokeRefusal, match="run id"):
        PaperSmokeTest(settings=settings, broker=b, run_id="paper")


def test_refuses_when_symbol_has_position_or_open_order(tmp_path):
    settings, b = make(tmp_path)
    b.submit_market_order("SPY", 1, "buy", "someone-else", "day")
    b.fill_all()
    rep = run(settings, b, tmp_path)
    assert not rep.ok and "already has a position" in rep.steps[-2].detail
    assert len(b.submitted) == 1, "no smoke order submitted"
    settings, b = make(tmp_path)
    b.submit_market_order("SPY", 1, "buy", "someone-else", "day")
    rep = run(settings, b, tmp_path, run_id="smoke-test-2")
    assert not rep.ok and "open order" in rep.steps[-2].detail


def test_refuses_paper_account_shape_mismatch(tmp_path):
    settings, b = make(tmp_path)
    b.account_number = "900000001"     # live-shaped number on a paper broker
    rep = run(settings, b, tmp_path)
    assert not rep.ok and "account number" in rep.steps[-2].detail and not b.submitted


def test_refuses_if_autonomous_flag_set(tmp_path):
    settings, b = make(tmp_path)
    s2 = settings.model_copy(update={"live_autonomous_trading": True})
    rep = PaperSmokeTest(settings=s2, broker=b, run_id="smoke-a", report_dir=tmp_path, sleep=lambda s: None, poll_seconds=0).run()
    assert not rep.ok and not b.submitted


def test_refuses_close_to_market_close(tmp_path):
    settings, b = make(tmp_path)
    b.market_open_override = True
    b.now = datetime(2026, 9, 28, 15, 55, tzinfo=NY)
    b.get_clock = lambda: __import__("bot.execution.broker", fromlist=["ClockInfo"]).ClockInfo(b.now, True, b.now + timedelta(hours=18), b.now + timedelta(minutes=5))
    rep = run(settings, b, tmp_path)
    assert not rep.ok and "closes in" in rep.steps[-2].detail and not b.submitted


def test_duplicate_that_creates_a_new_order_is_cancelled_and_fails(tmp_path):
    settings, b = make(tmp_path)
    real = b.submit_market_order

    def leaky(sym, qty, side, cid, tif="day"):
        if cid in b.orders:                      # a broken broker that accepts duplicates
            return real(sym, qty, side, cid + "-dupe", tif)
        return real(sym, qty, side, cid, tif)
    b.submit_market_order = leaky
    rep = run(settings, b, tmp_path)
    by = {s.name: s for s in rep.steps}
    assert by["duplicate_client_id_rejected"].status == FAIL and not rep.ok
    assert b.orders["smoke-test-1-SPY-entry-dupe"].status == "canceled"
    assert not b.get_open_orders(), "final checks cancel every leftover smoke order"


def test_cli_smoke_has_no_live_flag():
    from bot.cli import build_parser
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["smoke", "--live"])
    ns = p.parse_args(["smoke", "--paper", "--notional", "2", "--yes"])
    assert ns.notional == 2.0 and ns.symbol == "SPY"
