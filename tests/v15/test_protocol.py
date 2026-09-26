"""Research protocol: sealed holdout, unseal log, trial registry, deflated Sharpe, bootstrap, causal regimes, report, promotions."""
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.core.policy import RiskPolicy, promoted_modules
from bot.data.sessions import SessionCalendar
from bot.research import protocol as P
from bot.research.harness import classify_regimes
from tests.v15.helpers import minute_history

CAL = SessionCalendar()


def daily(seed, d0, d1, drift=0.0004, vol=0.012, base=100.0):
    days = [s.date for s in CAL.sessions_between(d0, d1)]
    r = np.random.default_rng(seed)
    n = len(days)
    c = base * np.exp(np.cumsum(r.normal(drift, vol, n)))
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d in days])
    return pd.DataFrame({"open": c * (1 + r.normal(0, 0.002, n)), "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)


def data_for(module, cut):
    s, e = P.CUTS[cut]
    d0 = date(s.year - 2, 1, 1)
    if module == "M2":
        days = [x.date for x in CAL.sessions_between(date(s.year, 1, 1), date(s.year, 2, 15))]
        return {"SPY": minute_history(days, base=400.0)}
    if module == "M3":
        out = {f"S{i}": daily(10 + i, d0, e, drift=0.0003, vol=0.02) for i in range(4)}
        out["SPY"] = daily(1, d0, e)
        return out
    return {"SPY": daily(1, d0, e), "QQQ": daily(2, d0, e, drift=0.0005, vol=0.016)}


def test_cuts_are_fixed_and_d_is_sealed():
    assert P.CUTS["A"][0] == date(2016, 1, 1) and P.CUTS["D"] == (date(2024, 1, 1), date(2026, 6, 30)) and P.SEALED_CUTS == {"D"}
    with pytest.raises(P.SealedHoldoutError):
        P.check_cut_access("D")
    with pytest.raises(P.SealedHoldoutError):
        P.check_cut_access("D", unseal=True, reason="   ")
    with pytest.raises(ValueError):
        P.check_cut_access("E")
    P.check_cut_access("B")   # no log, no error


def test_unsealing_is_logged_with_code_and_config_hashes(tmp_path):
    log = tmp_path / "UNSEAL_LOG.md"
    P.check_cut_access("D", unseal=True, reason="final validation before promotion", module="M1", params={"target_vol": 0.1}, log_path=log)
    text = log.read_text()
    assert "final validation before promotion" in text and "M1" in text and P.code_hash().split(":")[0] in text
    assert text.count("\n| 20") == 1
    P.check_cut_access("D", unseal=True, reason="again", module="M1", params={}, log_path=log)
    assert log.read_text().count("\n| 20") == 2


def test_run_trial_refuses_sealed_cut_before_touching_data(tmp_path):
    with pytest.raises(P.SealedHoldoutError):
        P.run_trial("M1", {}, "D", {}, registry=None)


def test_trial_registry_counts_distinct_parameter_sets(tmp_path):
    reg = P.TrialRegistry(tmp_path / "t.sqlite")
    m = {"sharpe": 0.5, "total_return": 0.1, "trade_count": 3, "max_drawdown": -0.05, "start": pd.Timestamp("2020-01-01")}
    reg.add("M2", "B", {"k": 0.5}, m)
    reg.add("M2", "B", {"k": 0.5}, {**m, "sharpe": 0.6})
    reg.add("M2", "B", {"k": 0.75}, m)
    reg.add("M2", "C", {"k": 0.5}, m, stress="spread_x2")
    assert reg.count("M2", "B") == 2 and reg.count("M2", "B", distinct_params=False) == 3 and reg.count("M2", "C") == 1
    assert reg.sharpes("M2", "B") == [0.5, 0.6, 0.5] and reg.sharpes("M2", "C") == []
    rows = reg.rows(module="M2")
    assert len(rows) == 4 and set(rows["cut"]) == {"B", "C"}


def test_deflated_sharpe_shrinks_with_trials_and_handles_edge_cases():
    one = P.deflated_sharpe(1.0, n_trials=1, n_obs=500)
    many = P.deflated_sharpe(1.0, n_trials=50, n_obs=500, trial_sharpes=list(np.linspace(-1, 1, 50)))
    assert 0.5 < one["dsr"] <= 1.0 and one["sr0_ann"] == 0.0
    assert many["sr0_ann"] > 0 and many["dsr"] < one["dsr"]
    assert math.isnan(P.deflated_sharpe(1.0, n_trials=3, n_obs=2)["dsr"])
    neg = P.deflated_sharpe(-0.5, n_trials=1, n_obs=500)
    assert neg["dsr"] < 0.5


def test_block_bootstrap_interval_contains_mean_and_narrows():
    rng = np.random.default_rng(0)
    small = rng.normal(1.0, 5.0, 30)
    big = rng.normal(1.0, 5.0, 3000)
    lo_s, m_s, hi_s = P.block_bootstrap_ci(small, n_boot=500)
    lo_b, m_b, hi_b = P.block_bootstrap_ci(big, n_boot=500)
    assert lo_s <= m_s <= hi_s and (hi_b - lo_b) < (hi_s - lo_s)
    assert P.block_bootstrap_ci([], n_boot=10)[0] != P.block_bootstrap_ci([], n_boot=10)[0]   # nan
    assert P.block_bootstrap_ci(big, n_boot=200, seed=1) == P.block_bootstrap_ci(big, n_boot=200, seed=1), "deterministic"


def test_regime_labels_are_causal():
    close = daily(3, date(2016, 1, 4), date(2020, 12, 31))["close"]
    full = classify_regimes(close)
    prefix = classify_regimes(close.iloc[:700])
    pd.testing.assert_frame_equal(full.iloc[:700], prefix, check_dtype=False)
    # the trend label is the sign of the trailing 126-day return
    t = 400
    assert bool(full["bull"].iloc[t]) == (close.iloc[t] > close.iloc[t - 126])
    assert not full[["high_vol", "low_vol"]].iloc[:80].any().any(), "no vol label before 63 past observations exist"


def test_evaluate_module_produces_full_report_and_registers_trials(tmp_path):
    reg = P.TrialRegistry(tmp_path / "t.sqlite")
    rep = P.evaluate_module("M1", data_for, registry=reg, surface=P.SURFACES["M1"][:2], n_boot=100)
    assert rep.status == "evaluated" and len(rep.criteria) == 5 and rep.passed in (True, False)
    assert len(rep.surface) == 2 and set(rep.stresses["stress"]) == set(P.STRESSES)
    assert reg.count("M1", "B") == 2 and reg.count("M1", "C", distinct_params=False) == len(P.STRESSES)
    assert rep.deflated["n_trials"] >= 1 and "overnight" in rep.attribution
    assert set(rep.regimes["regime"]) == {"bull", "bear", "sideways", "high_vol", "low_vol"}
    md = P.render_results([rep, P.ModuleReport("M2", "not_evaluated", reason="no minute bars")], registry=reg)
    assert "## M1" in md and "### Cost stress on C" in md and "NOT EVALUATED" in md and "## Trial registry" in md
    assert "not evidence of live profitability" in md


@pytest.mark.slow
def test_m2_and_m3_evaluate_through_the_protocol(tmp_path):
    reg = P.TrialRegistry(tmp_path / "t.sqlite")
    r2 = P.evaluate_module("M2", data_for, registry=reg, surface=P.SURFACES["M2"][:1], n_boot=50)
    r3 = P.evaluate_module("M3", data_for, registry=reg, surface=P.SURFACES["M3"][:1], n_boot=50,
                           symbol_kinds={f"S{i}": "stock" for i in range(4)})
    assert r2.status == "evaluated" and r3.status == "evaluated"
    assert r2.attribution.get("overnight", 0.0) == pytest.approx(0.0, abs=1e-6), "M2 never holds overnight"


def test_promotion_record_gates_live_policy(tmp_path):
    prom = tmp_path / "PROMOTIONS.md"
    prom.write_text("# promotions\n\n## M9 promoted 2026-01-01 by nobody\n")
    assert promoted_modules(prom) == {"M9"}
    assert promoted_modules(tmp_path / "missing.md") == set()
    live = RiskPolicy.load("config/policy.live.yaml")
    assert live.unpromoted_modules(prom) == ["M1"]
    with pytest.raises(ValueError, match="unpromoted"):
        RiskPolicy.load("config/policy.live.yaml", require_promotions=True, promotions_path=prom)
    prom.write_text("## M1 promoted 2026-09-30 after cut D\n")
    assert RiskPolicy.load("config/policy.live.yaml", require_promotions=True, promotions_path=prom).allowed_modules == ("M1",)
    assert RiskPolicy.load("config/policy.paper.yaml").unpromoted_modules(prom) == ["M2", "M3"], "legacy baselines are exempt"


def test_repo_promotions_file_promotes_nothing_yet():
    assert promoted_modules() == set()
    assert P.code_hash().count(":") == 1 and len(P.params_hash({"a": 1})) == 16
