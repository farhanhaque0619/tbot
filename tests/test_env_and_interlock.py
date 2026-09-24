"""Phase 1 / 4 / 5: credential separation, environment consistency, live interlock, safe-live caps."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot.config import LIVE_CONFIRMATION_PHRASE, Settings, banner
from bot.data.calendar import NY
from bot.data.loader import BarLoader
from bot.data.store import BarStore
from bot.execution import FakeBroker, LiveInterlock, StateStore, Trader
from bot.execution.broker import AssetInfo
from bot.monitoring.alerts import Alerter
from bot.monitoring.decisions import DecisionLog
from bot.risk import RiskLimits
from bot.strategies import MACrossover
from tests.conftest import make_bars

PK = dict(alpaca_paper_api_key="PKTESTPAPERKEY", alpaca_paper_secret_key="papersecretpapersecret")
AK = dict(alpaca_live_api_key="AKTESTLIVEKEY", alpaca_live_secret_key="livesecretlivesecret")


def S(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ---------------------------------------------------------------- credentials
def test_no_generic_key_pair_and_defaults():
    s = S()
    assert not hasattr(s, "alpaca_api_key") and not hasattr(s, "live_trading")
    assert s.trading_env == "paper" and s.safe_live_test_mode and not s.live_autonomous_trading and not s.enable_jev and not s.enable_kelly


def test_missing_credentials_raise_named_error():
    with pytest.raises(RuntimeError, match="ALPACA_PAPER_API_KEY"):
        S().credentials("paper")
    with pytest.raises(RuntimeError, match="ALPACA_LIVE_API_KEY"):
        S(**PK).credentials("live")


def test_live_key_in_paper_slot_is_refused_and_vice_versa():
    with pytest.raises(RuntimeError, match="looks like a LIVE key"):
        S(alpaca_paper_api_key="AKOOPS", alpaca_paper_secret_key="x" * 20).credentials("paper")
    with pytest.raises(RuntimeError, match="looks like a PAPER key"):
        S(alpaca_live_api_key="PKOOPS", alpaca_live_secret_key="x" * 20).credentials("live")
    # explicit opt-out exists but is off by default
    s = S(alpaca_paper_api_key="AKOOPS", alpaca_paper_secret_key="x" * 20, strict_key_prefix_check=False)
    assert s.credentials("paper")[0] == "AKOOPS"
    assert S(alpaca_live_api_key="PKOOPS", alpaca_live_secret_key="x").credential_status("live")["key_prefix_ok"] is False


def test_secret_values_never_in_repr_and_all_secrets_redactable():
    s = S(**PK, **AK, discord_webhook_url="https://discord/hook/abcdefgh", jev_api_key="jevsecret123")
    text = repr(s) + str(s) + json.dumps(s.model_dump(mode="json"), default=str)
    for v in s.secret_values():
        assert v not in text
    assert len(s.secret_values()) == 6


def test_alpaca_broker_for_env_selects_host_without_network():
    from bot.execution.broker import AlpacaBroker
    b = AlpacaBroker.for_env(S(**PK), "paper")
    assert b.is_paper and b.env == "paper" and "paper-api" in b.base_url
    b = AlpacaBroker.for_env(S(**AK), "live")
    assert not b.is_paper and b.env == "live" and "paper-api" not in b.base_url and b.base_url.startswith("https://api.alpaca")
    with pytest.raises(RuntimeError):
        AlpacaBroker.for_env(S(**PK), "live")          # live env, only paper keys
    with pytest.raises(ValueError):
        AlpacaBroker(S(**PK), env="prod")              # type: ignore[arg-type]


def test_banner_text():
    assert "PAPER" in banner("paper") and "REAL MONEY" not in banner("paper")
    assert "LIVE — REAL MONEY" in banner("live") and banner("live").startswith("!!!!")


# ------------------------------------------------------------ trader env guards
def _env(tmp_path, *, env="paper", cash=100_000.0, settings=None):
    closes = 100 + np.arange(300) * 0.2
    df = make_bars(300, seed=1, closes=closes)
    df.index = pd.bdate_range(end="2024-01-05", periods=300, tz=NY)
    store = BarStore()
    store.upsert_bars("SPY", df)
    store.set_coverage("SPY", df.index[0].date(), df.index[-1].date())
    price = float(df["close"].iloc[-1])
    broker = FakeBroker(cash=cash, prices={"SPY": price, "QQQ": price / 2}, env=env,
                        assets={"SPY": AssetInfo("SPY", True, True, True, True, True), "QQQ": AssetInfo("QQQ", True, True, True, True, True)})
    settings = settings or S()
    alerter = Alerter()

    def make(**kw):
        return Trader(settings=settings, broker=broker, loader=BarLoader(store, None), bar_store=store, strategy_cls=MACrossover,
                      params={"fast": 10, "slow": 50}, symbols=kw.pop("symbols", ["SPY"]),
                      state_store=StateStore(tmp_path / f"{env}.json"), alerter=alerter, env=env, run_id=env,
                      decision_log=DecisionLog(tmp_path / "decisions.jsonl"), **kw)
    return {"make": make, "broker": broker, "store": store, "alerter": alerter, "price": price, "settings": settings, "tmp": tmp_path}


def test_trader_refuses_broker_env_mismatch(tmp_path):
    e = _env(tmp_path, env="paper")
    with pytest.raises(RuntimeError, match="does not match"):
        Trader(settings=S(), broker=FakeBroker(env="live"), loader=BarLoader(e["store"], None), bar_store=e["store"],
               strategy_cls=MACrossover, params={}, symbols=["SPY"], state_store=StateStore(tmp_path / "x.json"), env="paper")


def test_trader_refuses_state_file_from_other_env(tmp_path):
    e = _env(tmp_path, env="paper")
    e["make"]().run_cycle(datetime(2024, 1, 5, 19, 30, tzinfo=NY))
    live_settings = S(trading_env="live", **AK)
    with pytest.raises(RuntimeError, match="belongs to env"):
        Trader(settings=live_settings, broker=FakeBroker(env="live", prices={"SPY": 1.0}), loader=BarLoader(e["store"], None),
               bar_store=e["store"], strategy_cls=MACrossover, params={"fast": 10, "slow": 50}, symbols=["SPY"],
               state_store=StateStore(tmp_path / "paper.json"), env="live")


def test_live_loop_refuses_without_autonomous_flag(tmp_path):
    e = _env(tmp_path, env="live", cash=100.0, settings=S(trading_env="live", **AK))
    with pytest.raises(PermissionError, match="LIVE_AUTONOMOUS_TRADING"):
        e["make"](cli_live_flag=True).run_forever(1)


# ------------------------------------------------------------------- interlock
def test_interlock_arm_requirements_and_expiry(tmp_path):
    base = dict(state_dir=tmp_path)
    il = LiveInterlock(S(**base))                      # paper env, no live creds
    with pytest.raises(PermissionError, match="phrase"):
        il.arm(typed_phrase="yes", acknowledged=True)
    with pytest.raises(PermissionError, match="acknowledged"):
        il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=False)
    with pytest.raises(PermissionError, match="credentials"):
        il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True)
    with pytest.raises(PermissionError, match="TRADING_ENV"):
        LiveInterlock(S(**base, **AK)).arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True)
    il = LiveInterlock(S(**base, **AK, trading_env="live"))
    assert il.is_armed()[0] is False
    st = il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True, ttl_minutes=1)
    assert il.is_armed()[0] is True and st.confirmed_phrase
    # changing a safe-mode limit invalidates the arm
    il2 = LiveInterlock(S(**base, **AK, trading_env="live", safe_max_order_notional=999))
    assert il2.is_armed() == (False, "safe-mode limits changed since arming; re-arm required")
    # expiry
    data = json.loads(il.path.read_text())
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    il.path.write_text(json.dumps(data))
    assert il.is_armed()[0] is False and "expired" in il.is_armed()[1]
    il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True)
    il.clear_on_startup()
    assert il.is_armed()[0] is False and not il.path.exists()
    assert il.disarm() is False


def test_interlock_gates_all_required():
    il = LiveInterlock(S(**AK, trading_env="live"))
    from bot.execution.broker import AccountInfo
    acct = AccountInfo(100, 100, 100, account_number="900001", status="ACTIVE")
    from bot.risk import RiskManager
    gates = il.check(cli_live_flag=True, account=acct, account_env_ok=(True, "ok"), data_fresh=True, risk_manager=RiskManager(RiskLimits()))
    failed = [g.name for g in gates if not g.ok]
    assert failed == ["operator_confirmation_and_armed"]
    blocked = AccountInfo(100, 100, 100, account_number="900001", status="ACTIVE", trading_blocked=True)
    gates = il.check(cli_live_flag=False, account=blocked, account_env_ok=(False, "paper"), data_fresh=False, risk_manager=None)
    failed = {g.name for g in gates if not g.ok}
    assert {"cli_live_flag", "account_is_not_paper", "trading_not_blocked", "market_data_fresh", "risk_manager_healthy",
            "kill_switch_clear", "operator_confirmation_and_armed"} <= failed


# --------------------------------------------------- live trader end to end (fake)
def _live_env(tmp_path, **overrides):
    settings = S(trading_env="live", state_dir=tmp_path, safe_allowed_symbols="SPY", **AK, **overrides)
    e = _env(tmp_path, env="live", cash=100.0, settings=settings)
    e["broker"].now = datetime(2024, 1, 8, 10, 0, tzinfo=NY)   # Monday, market open -> fractional DAY orders possible
    return e


def test_live_trader_starts_disarmed_and_blocks_orders(tmp_path):
    e = _live_env(tmp_path)
    il = LiveInterlock(e["settings"])
    il.arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True)
    t = e["make"](cli_live_flag=True)                 # process start clears the arm
    assert il.is_armed()[0] is False
    s = t.run_cycle(datetime(2024, 1, 8, 10, 0, tzinfo=NY))
    assert s["actions"] == [] and not e["broker"].submitted
    recs = DecisionLog(tmp_path / "decisions.jsonl").read()
    assert recs[-1]["order_decision"] == "blocked" and "interlock" in recs[-1]["notes"][-1]
    assert recs[-1]["risk_decision"]["approved"] is True   # risk approved; the interlock blocked


def test_live_trader_armed_submits_fractional_day_order_within_safe_caps(tmp_path):
    e = _live_env(tmp_path)
    t = e["make"](cli_live_flag=True)
    LiveInterlock(e["settings"]).arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True)   # after start
    s = t.run_cycle(datetime(2024, 1, 8, 10, 0, tzinfo=NY))
    assert len(e["broker"].submitted) == 1
    o = e["broker"].submitted[0]
    assert o.side == "buy" and o.time_in_force == "day" and o.qty * e["price"] <= 25.0 + 1e-6 and o.qty != int(o.qty)
    assert o.client_order_id == "live-SPY-2024-01-05-entry"
    assert s["actions"][0].startswith("buy")


def test_safe_mode_blocks_second_position_and_shorts(tmp_path):
    e = _live_env(tmp_path)
    e["make"]   # noqa: B018
    t = Trader(settings=e["settings"], broker=e["broker"], loader=BarLoader(e["store"], None), bar_store=e["store"],
               strategy_cls=MACrossover, params={"fast": 10, "slow": 50}, symbols=["SPY", "QQQ"],
               state_store=StateStore(tmp_path / "live.json"), env="live", run_id="live", cli_live_flag=True,
               decision_log=DecisionLog(tmp_path / "d.jsonl"))
    e["store"].upsert_bars("QQQ", e["store"].get_bars("SPY") / 2)
    e["store"].set_coverage("QQQ", e["store"].get_bars("SPY").index[0].date(), e["store"].get_bars("SPY").index[-1].date())
    LiveInterlock(e["settings"]).arm(typed_phrase=LIVE_CONFIRMATION_PHRASE, acknowledged=True)
    t.run_cycle(datetime(2024, 1, 8, 10, 0, tzinfo=NY))
    subs = [o.symbol for o in e["broker"].submitted]
    assert subs == ["SPY"], "QQQ must be blocked: not in SAFE_ALLOWED_SYMBOLS and max_positions=1"
    recs = DecisionLog(tmp_path / "d.jsonl").read()
    qqq = [r for r in recs if r["symbol"] == "QQQ"][-1]
    assert qqq["order_decision"] == "blocked"
    # blocked either by the stateful position cap (before sizing) or by the pre-trade gate
    assert (qqq["risk_decision"] or {}).get("code") in ("safe_symbol_allowed", "position_limit") or any("max positions" in n for n in qqq["notes"])


def test_safe_mode_kill_switch_in_dollars(tmp_path):
    e = _live_env(tmp_path, safe_max_account_drawdown=3.0)
    t = e["make"](cli_live_flag=True)
    t.run_cycle(datetime(2024, 1, 8, 10, 0, tzinfo=NY))
    e["broker"].cash -= 3.5                       # simulate a $3.50 loss on a $100 account
    s = t.run_cycle(datetime(2024, 1, 8, 10, 5, tzinfo=NY))
    assert s["status"] == "killed" and t.state.risk["killed"] and "SAFE mode" in t.state.risk["kill_reason"]


def test_state_file_corruption_is_recovered_not_overwritten(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    st = StateStore(p).load()
    assert st.recovered_from_corruption and st.last_error and not p.exists()
    assert list(tmp_path.glob("s.json.corrupt-*"))
    p.write_text("[1,2,3]")
    assert StateStore(p).load().recovered_from_corruption
