import pandas as pd
import pytest

from bot.core.policy import RiskPolicy
from bot.portfolio.allocator import Allocator, PositionView
from tests.v15.helpers import intent

POL = RiskPolicy(name="t", allowed_modules=("A", "B", "M1"), max_symbol_exposure_pct=0.5, max_module_gross_pct={"A": 0.4}, max_gross_pct=1.0,
                 max_net_pct=1.0, min_net_pct=-0.5, max_sector_pct_of_gross=1.0, allow_short=True, allow_margin=True)


def test_vol_normalised_notional_and_rounding():
    a = Allocator(POL, whole_share_capable=False)
    res = a.allocate([intent(module="A", vol=0.01, risk=0.01, price=100.0)], [], 100_000)
    t = res.targets[0]
    # raw vol-normalised notional is 100k * 0.01 / 0.01 = 100k; module A gross cap (40%) binds first
    assert t.notional == pytest.approx(40_000) and t.target_qty == pytest.approx(400.0)
    assert any("module A gross cap" in x for x in t.scaling_log)
    # small risk budget: 100k * 0.002 / 0.02 = 10k, nothing binds, fractional qty allowed
    res = a.allocate([intent(module="A", vol=0.02, risk=0.002, price=33.0)], [], 100_000)
    assert res.targets[0].notional == pytest.approx(10_000, rel=1e-4) and res.targets[0].target_qty == pytest.approx(10_000 / 33.0, rel=1e-4)
    assert not any("cap" in x for x in res.targets[0].scaling_log)
    # whole-share rounding
    res = Allocator(POL, whole_share_capable=True).allocate([intent(module="A", vol=0.02, risk=0.002, price=33.0)], [], 100_000)
    assert res.targets[0].target_qty == 303


def test_symbol_cap_accounts_for_existing_exposure_of_other_modules():
    a = Allocator(POL, whole_share_capable=False)
    existing = [PositionView("SPY", "B", 300, 100.0)]        # $30k held by B
    res = a.allocate([intent(module="A", vol=0.01, risk=0.01, price=100.0)], existing, 100_000)
    assert res.targets[0].notional == pytest.approx(20_000), "50% symbol cap minus existing 30k"


def test_sector_gross_and_net_caps_scale_new_intents_only():
    pol = POL.model_copy(update={"max_module_gross_pct": {}, "max_sector_pct_of_gross": 0.3})
    a = Allocator(pol, sectors={"XLK": "tech", "AAPL": "tech"}, whole_share_capable=False)
    res = a.allocate([intent("XLK", "A", vol=0.01, risk=0.01, price=100.0), intent("AAPL", "B", vol=0.01, risk=0.01, price=100.0)], [], 100_000)
    total = sum(t.notional for t in res.targets)
    assert total == pytest.approx(30_000) and all(any("sector tech cap" in x for x in t.scaling_log) for t in res.targets)
    # gross cap with existing position
    pol2 = POL.model_copy(update={"max_module_gross_pct": {}, "max_sector_pct_of_gross": 1.0, "max_symbol_exposure_pct": 1.0})
    a2 = Allocator(pol2, whole_share_capable=False)
    res = a2.allocate([intent("SPY", "A", vol=0.01, risk=0.01, price=100.0)], [PositionView("QQQ", "B", 800, 100.0)], 100_000)
    assert res.targets[0].notional == pytest.approx(20_000), "gross cap 100% minus 80k existing"
    # net cap: a short intent hitting min_net -50%
    res = a2.allocate([intent("SPY", "A", direction=-1, vol=0.01, risk=0.01, price=100.0)], [], 100_000)
    assert res.targets[0].target_qty == pytest.approx(-500)


def test_cash_cap_when_margin_not_allowed():
    pol = POL.model_copy(update={"allow_margin": False, "max_module_gross_pct": {}, "max_sector_pct_of_gross": 1.0})
    a = Allocator(pol, whole_share_capable=False)
    res = a.allocate([intent(module="A", vol=0.01, risk=0.01, price=100.0)], [], 100_000, cash=10_000)
    assert res.targets[0].notional == pytest.approx(10_000)


def test_correlation_haircut_netting_and_min_notional():
    pol = POL.model_copy(update={"max_module_gross_pct": {}, "max_sector_pct_of_gross": 1.0})
    a = Allocator(pol, whole_share_capable=False)
    corr = pd.DataFrame([[1, 0.8], [0.8, 1]], index=["SPY", "QQQ"], columns=["SPY", "QQQ"])
    res = a.allocate([intent("SPY", "A", vol=0.02, risk=0.002, price=100.0), intent("QQQ", "B", vol=0.02, risk=0.002, price=100.0)], [], 100_000, corr_matrix=corr)
    assert all(t.notional == pytest.approx(10_000 * 0.7) for t in res.targets)
    # two modules on one symbol net
    res = a.allocate([intent("SPY", "A", vol=0.02, risk=0.002, price=100.0), intent("SPY", "B", direction=-1, vol=0.02, risk=0.002, price=100.0)], [], 100_000)
    assert res.netted_qty["SPY"] == pytest.approx(0.0) and len(res.targets) == 2
    # $1 minimum
    res = a.allocate([intent("SPY", "A", vol=1.0, risk=0.0000001, price=100.0)], [], 100_000)
    assert res.targets == [] and any("dropped" in x for x in res.log)


def test_m1_target_weight_path_and_exit_intent():
    a = Allocator(POL.model_copy(update={"max_module_gross_pct": {}, "max_sector_pct_of_gross": 1.0, "max_symbol_exposure_pct": 1.0}), whole_share_capable=True)
    res = a.allocate([intent("SPY", "M1", vol=0.15, risk=0.0, price=500.0, weight=0.42, style="cls")], [], 100_000)
    t = res.targets[0]
    assert t.target_qty == 84 and "rounded to whole shares" in t.scaling_log[-1]
    res = a.allocate([intent("SPY", "M1", direction=0, vol=0.15, risk=0.0, price=500.0)], [PositionView("SPY", "M1", 84, 500.0)], 100_000)
    assert res.targets[0].kind == "exit" and res.targets[0].is_flat
