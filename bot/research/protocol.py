"""Research protocol (V1.5 Phase 3, spec §11). Brain layer: never imports anything that can place an order.

Fixed cuts A (exploration), B (development), C (validation), D (holdout, SEALED). Every backtest that runs through
``run_trial`` is appended to the trial registry (research/trials.sqlite); reports deflate the Sharpe ratio by the number
of trials on the same module and cut. ``evaluate_module`` produces the required report per module: parameter surface on
B, validation on C with a block-bootstrap interval of the net expectancy, cost stress on C, causal regime table on C,
overnight/intraday attribution, deflated Sharpe and a plain-English pass/fail against the fixed criteria. Cut D refuses
to run without ``unseal=True`` and a reason, and every unsealing is logged with code and config hashes.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable

import numpy as np
import pandas as pd

from bot.backtest.engine_v15 import BacktestResultV15, MinuteRunConfig, run_daily_legacy, run_daily_v15, run_minute
from bot.backtest.fills import FillParams
from bot.core.policy import RiskPolicy
from bot.data.sessions import SessionCalendar
from bot.research.harness import classify_regimes, regime_table

ROOT = Path(__file__).resolve().parents[2]
RESEARCH_DIR = ROOT / "research"

# ------------------------------------------------------------------------------------------------- fixed cuts
CUTS: dict[str, tuple[date, date]] = {
    "A": (date(2016, 1, 1), date(2018, 12, 31)),
    "B": (date(2019, 1, 1), date(2021, 12, 31)),
    "C": (date(2022, 1, 1), date(2023, 12, 31)),
    "D": (date(2024, 1, 1), date(2026, 6, 30)),
}
SEALED_CUTS = frozenset({"D"})
CUT_ROLE = {"A": "exploration", "B": "development", "C": "validation", "D": "holdout (sealed)"}

# declared parameter surfaces (spec §11: no hand-chosen walk-forward grids)
SURFACES: dict[str, list[dict[str, Any]]] = {
    "M1": [{"target_vol": tv, "cap": cap} for tv in (0.08, 0.10, 0.12) for cap in (0.5, 0.6)],
    "M2": [{"k": k, "agreement": a, "entry_time": et} for k in (0.25, 0.5, 0.75, 1.0) for a in (False, True) for et in ("15:15", "15:30", "15:45")],
    "M3": [{"n_bottom": n, "hold_sessions": h} for n in (2, 3, 5) for h in (3, 5, 7)],
}
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {"M1": {"target_vol": 0.10, "cap": 0.6}, "M2": {"k": 0.5, "agreement": False, "entry_time": "15:30"},
                                            "M3": {"n_bottom": 3, "hold_sessions": 5}}
STRESSES: dict[str, FillParams] = {
    "base": FillParams(),
    "spread_x2": FillParams(spread_mult=2.0),
    "slippage_x2": FillParams(slippage_mult=2.0),
    "delay_1bar": FillParams(execution_delay_bars=1),
    "drop_10pct": FillParams(drop_signal_fraction=0.10, seed=1),
    "adverse_2bps_20pct": FillParams(adverse_fill_prob=0.20, adverse_fill_bps=2.0, seed=2),
}
MECHANISM = {"M1": "overnight", "M2": "intraday", "M3": "overnight", "ma_crossover": "overnight", "mean_reversion": "overnight"}
MINUTE_MODULES = {"M2"}
CODE_FILES = ["bot/strategies/v15", "bot/backtest/engine_v15.py", "bot/backtest/fills.py", "bot/backtest/simbroker.py", "bot/portfolio/allocator.py",
              "bot/risk/policy_engine.py", "bot/execution/oms.py", "bot/features/engine.py", "bot/research/protocol.py"]


class SealedHoldoutError(PermissionError):
    pass


def code_hash() -> str:
    h = hashlib.sha256()
    for rel in CODE_FILES:
        p = ROOT / rel
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            if f.exists():
                h.update(f.relative_to(ROOT).as_posix().encode()); h.update(f.read_bytes())
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        head = "nogit"
    return f"{head}:{h.hexdigest()[:16]}"


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def data_hash(data: dict[str, pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for s in sorted(data):
        df = data[s]
        h.update(s.encode()); h.update(str(len(df)).encode())
        if len(df):
            h.update(str(df.index[0]).encode()); h.update(str(df.index[-1]).encode()); h.update(f"{float(df['close'].sum()):.6f}".encode())
    return h.hexdigest()[:16]


def check_cut_access(cut: str, *, unseal: bool = False, reason: str | None = None, module: str = "", params: dict | None = None,
                     log_path: str | Path | None = None) -> None:
    """Refuse the sealed holdout unless explicitly unsealed with a reason; log every unsealing."""
    if cut not in CUTS:
        raise ValueError(f"unknown cut {cut!r}; cuts are {sorted(CUTS)}")
    if cut not in SEALED_CUTS:
        return
    if not unseal or not (reason or "").strip():
        raise SealedHoldoutError(f"cut {cut} is the sealed holdout: pass --unseal --reason \"<text>\" to run it; the unsealing is logged")
    p = Path(log_path) if log_path else RESEARCH_DIR / "UNSEAL_LOG.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("# UNSEAL LOG — every run on the sealed holdout (cut D)\n\n| when (UTC) | module | params | code hash | config hash | reason |\n|---|---|---|---|---|---|\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"| {datetime.now(timezone.utc).isoformat(timespec='seconds')} | {module} | {params_hash(params or {})} | {code_hash()} | "
                f"{params_hash({'cut': cut, 'params': params or {}})} | {reason.strip()} |\n")


# ------------------------------------------------------------------------------------------------- registry
class TrialRegistry:
    """Append-only registry of every protocol backtest (research/trials.sqlite)."""

    def __init__(self, path: str | Path = RESEARCH_DIR / "trials.sqlite"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path))
        self.con.execute("""CREATE TABLE IF NOT EXISTS trials (id INTEGER PRIMARY KEY, ts TEXT, module TEXT, cut TEXT, params_hash TEXT,
                            params TEXT, code_hash TEXT, data_hash TEXT, stress TEXT, sharpe REAL, total_return REAL, trades INTEGER, metrics TEXT)""")
        self.con.execute("CREATE INDEX IF NOT EXISTS ix_trials_module_cut ON trials(module, cut)")
        self.con.commit()

    def add(self, module: str, cut: str, params: dict[str, Any], metrics: dict[str, Any], *, stress: str = "base", code: str = "", data: str = "") -> int:
        clean = {k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v)) for k, v in metrics.items()}
        cur = self.con.execute("INSERT INTO trials (ts, module, cut, params_hash, params, code_hash, data_hash, stress, sharpe, total_return, trades, metrics) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (datetime.now(timezone.utc).isoformat(timespec="seconds"), module, cut, params_hash(params), json.dumps(params, sort_keys=True, default=str),
                                code, data, stress, float(metrics.get("sharpe", 0.0)), float(metrics.get("total_return", 0.0)), int(metrics.get("trade_count", 0)),
                                json.dumps(clean, default=str)))
        self.con.commit()
        return int(cur.lastrowid)

    def count(self, module: str, cut: str, *, distinct_params: bool = True) -> int:
        q = "SELECT COUNT(DISTINCT params_hash) FROM trials WHERE module=? AND cut=?" if distinct_params else "SELECT COUNT(*) FROM trials WHERE module=? AND cut=?"
        return int(self.con.execute(q, (module, cut)).fetchone()[0])

    def sharpes(self, module: str, cut: str) -> list[float]:
        return [float(r[0]) for r in self.con.execute("SELECT sharpe FROM trials WHERE module=? AND cut=? AND stress='base'", (module, cut)).fetchall()]

    def rows(self, module: str | None = None, cut: str | None = None) -> pd.DataFrame:
        q, args = "SELECT ts, module, cut, params_hash, params, code_hash, stress, sharpe, total_return, trades FROM trials", []
        conds = []
        if module:
            conds.append("module=?"); args.append(module)
        if cut:
            conds.append("cut=?"); args.append(cut)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return pd.read_sql_query(q + " ORDER BY id", self.con, params=args)

    def close(self) -> None:
        self.con.close()


# ------------------------------------------------------------------------------------------------- statistics
def deflated_sharpe(sharpe_ann: float, *, n_trials: int, n_obs: int, skew: float = 0.0, kurt: float = 3.0, trial_sharpes: list[float] | None = None,
                    periods: int = 252) -> dict[str, float]:
    """Bailey & López de Prado deflated Sharpe ratio. Inputs are annualised; the test runs per period.

    SR0 = sqrt(V[SR]) * ((1 - g) * Z(1 - 1/N) + g * Z(1 - 1/(N e))), g = Euler-Mascheroni; V[SR] is the variance of the
    trial Sharpes (per period) when >= 2 trials are known, else 0 (then DSR = PSR against zero).
    DSR = Phi( (SR - SR0) * sqrt(T - 1) / sqrt(1 - skew * SR + (kurt - 1) / 4 * SR^2) ).
    """
    nd = NormalDist()
    sr = sharpe_ann / math.sqrt(periods)
    n = max(int(n_trials), 1)
    var = float(np.var([s / math.sqrt(periods) for s in trial_sharpes], ddof=1)) if trial_sharpes and len(trial_sharpes) >= 2 else 0.0
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * nd.inv_cdf(1 - 1 / n) + g * nd.inv_cdf(1 - 1 / (n * math.e))) if n > 1 and var > 0 else 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if n_obs < 3 or denom <= 0:
        return {"sharpe_ann": sharpe_ann, "sr0_ann": sr0 * math.sqrt(periods), "dsr": float("nan"), "n_trials": n}
    z = (sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return {"sharpe_ann": sharpe_ann, "sr0_ann": sr0 * math.sqrt(periods), "dsr": float(nd.cdf(z)), "n_trials": n}


def block_bootstrap_ci(x: np.ndarray | list[float], *, block: int = 5, n_boot: int = 2000, alpha: float = 0.10, seed: int = 0) -> tuple[float, float, float]:
    """(lower, mean, upper) of the mean of ``x`` by circular block bootstrap; alpha=0.10 gives the 90% interval."""
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return float("nan"), float("nan"), float("nan")
    if len(x) < 3:
        return float(x.mean()), float(x.mean()), float(x.mean())
    rng = np.random.default_rng(seed)
    n = len(x)
    block = max(1, min(block, n))
    nblocks = math.ceil(n / block)
    means = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, nblocks)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        means[i] = x[idx].mean()
    return float(np.quantile(means, alpha / 2)), float(x.mean()), float(np.quantile(means, 1 - alpha / 2))


def moments(returns: pd.Series) -> tuple[float, float]:
    r = returns.dropna()
    if len(r) < 4 or r.std(ddof=1) == 0:
        return 0.0, 3.0
    return float(r.skew()), float(r.kurt() + 3.0)


# ------------------------------------------------------------------------------------------------- running
DataFor = Callable[[str, str], dict[str, pd.DataFrame]]      # (module, cut) -> {symbol: bars}; minute bars for M2, daily otherwise


@dataclass
class TrialResult:
    module: str
    cut: str
    params: dict[str, Any]
    stress: str
    result: BacktestResultV15
    metrics: dict[str, Any]
    trial_id: int | None = None


def build_module(module_id: str, params: dict[str, Any], *, symbols: list[str], fractional: bool = False):
    from bot.strategies.v15 import MODULES
    if module_id in MODULES:
        return MODULES[module_id](symbols, fractional=fractional, **params)
    from bot.strategies import STRATEGIES
    if module_id in STRATEGIES:
        return STRATEGIES[module_id](**params)
    raise KeyError(f"unknown module {module_id!r}")


def run_trial(module_id: str, params: dict[str, Any], cut: str, data: dict[str, pd.DataFrame], *, calendar: SessionCalendar | None = None,
              policy: RiskPolicy | None = None, fills: FillParams | None = None, stress: str = "base", registry: TrialRegistry | None = None,
              fractional: bool = False, symbols: list[str] | None = None, symbol_kinds: dict[str, str] | None = None, sectors: dict[str, str] | None = None,
              warmup_sessions: int = 260, unseal: bool = False, reason: str | None = None, unseal_log: str | Path | None = None,
              initial_cash: float = 100_000.0) -> TrialResult:
    """One protocol backtest of a module on a cut; registered in the trial registry when one is given."""
    check_cut_access(cut, unseal=unseal, reason=reason, module=module_id, params=params, log_path=unseal_log)
    calendar = calendar or SessionCalendar()
    policy = policy or RiskPolicy.load(ROOT / "config/policy.paper.yaml")
    start, end = CUTS[cut]
    syms = [s.upper() for s in (symbols or list(data))]
    cfg = MinuteRunConfig(initial_cash=initial_cash, fills=fills or STRESSES.get(stress, FillParams()), whole_share_capable=not fractional,
                          symbol_kinds=symbol_kinds or {}, sectors=sectors or {}, run_id=f"rp-{module_id}-{cut}", throttle=False)
    from bot.strategies import STRATEGIES
    if module_id in STRATEGIES:
        res = run_daily_legacy(lambda: STRATEGIES[module_id](**params), data, initial_cash=initial_cash, fills=cfg.fills, policy=policy,
                               trade_start=pd.Timestamp(start, tz="America/New_York"), trade_end=pd.Timestamp(end, tz="America/New_York") + pd.Timedelta(hours=23),
                               benchmark=False, run_id=cfg.run_id)
    else:
        mod = build_module(module_id, params, symbols=syms, fractional=fractional)
        if module_id in MINUTE_MODULES:
            res = run_minute([mod], data, calendar=calendar, policy=policy, start=start, end=end, config=cfg)
        else:
            res = run_daily_v15([mod], data, calendar=calendar, policy=policy, start=start, end=end, config=cfg, warmup_sessions=warmup_sessions)
    m = dict(res.metrics)
    m["attribution"] = res.attribution.get(module_id, {})
    tr = TrialResult(module_id, cut, dict(params), stress, res, m)
    if registry is not None:
        tr.trial_id = registry.add(module_id, cut, params, m, stress=stress, code=code_hash(), data=data_hash(data))
    return tr


# ------------------------------------------------------------------------------------------------- evaluation
CRITERIA = [
    "surface: median Sharpe on B > 0 and >= 70% of the best point's neighbours within 30% of the best",
    "validation: block-bootstrap 90% lower bound of the net expectancy per trade on C > 0",
    "stress: every cost stress on C leaves the point estimate positive and within 60% of base",
    "regimes: no causal regime with Sharpe < -0.5 covering > 25% of C",
    "attribution: P&L consistent with the module's mechanism (M2 intraday; M1/M3 overnight)",
]


@dataclass
class ModuleReport:
    module: str
    status: str                                  # "evaluated" | "not_evaluated"
    reason: str = ""
    surface: pd.DataFrame | None = None
    surface_summary: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    stresses: pd.DataFrame | None = None
    regimes: pd.DataFrame | None = None
    attribution: dict[str, float] = field(default_factory=dict)
    deflated: dict[str, float] = field(default_factory=dict)
    criteria: list[tuple[str, bool | None, str]] = field(default_factory=list)
    data_note: str = ""
    code: str = ""

    @property
    def passed(self) -> bool | None:
        if self.status != "evaluated":
            return None
        return all(ok is True for _, ok, _ in self.criteria)


def _neighbours(params: dict[str, Any], grid: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for key in params:
        vals = sorted({g[key] for g in grid if all(g[k] == params[k] for k in params if k != key)}, key=lambda v: (str(type(v)), v))
        if params[key] not in vals:
            continue
        i = vals.index(params[key])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                cand = dict(params); cand[key] = vals[j]
                if cand in grid:
                    out.append(cand)
    return out


def evaluate_module(module_id: str, data_for: DataFor, *, registry: TrialRegistry | None = None, surface: list[dict[str, Any]] | None = None,
                    params: dict[str, Any] | None = None, n_boot: int = 2000, benchmark_symbol: str = "SPY", **run_kw) -> ModuleReport:
    """The required report for one module: surface on B, validation + bootstrap + stresses + regimes + attribution on C."""
    rep = ModuleReport(module_id, "evaluated", code=code_hash())
    grid = surface if surface is not None else SURFACES.get(module_id) or [params or DEFAULT_PARAMS.get(module_id, {})]
    base_params = params or DEFAULT_PARAMS.get(module_id, grid[0])
    try:
        data_b, data_c = data_for(module_id, "B"), data_for(module_id, "C")
    except Exception as e:  # noqa: BLE001
        return ModuleReport(module_id, "not_evaluated", reason=f"no data: {type(e).__name__}: {e}", code=rep.code)
    if not data_b or not data_c or any(df.empty for df in list(data_b.values()) + list(data_c.values())):
        return ModuleReport(module_id, "not_evaluated", reason="no bars for cut B or C", code=rep.code)
    rep.data_note = f"B: {data_hash(data_b)}  C: {data_hash(data_c)}"
    # ---- surface on B
    rows = []
    for p in grid:
        tr = run_trial(module_id, p, "B", data_b, registry=registry, **run_kw)
        rows.append({**p, "sharpe": tr.metrics["sharpe"], "total_return": tr.metrics["total_return"], "max_drawdown": tr.metrics["max_drawdown"],
                     "trades": tr.metrics["trade_count"], "exposure": tr.metrics.get("exposure", 0.0)})
    surf = pd.DataFrame(rows)
    for k in grid[0]:
        surf[k] = surf[k].astype(object)      # keep declared parameter values as written (ints stay ints in the report)
    rep.surface = surf
    best_row = surf.sort_values("sharpe", ascending=False).iloc[0]
    best = {k: best_row[k] for k in grid[0]}
    best = {k: (v.item() if hasattr(v, "item") else v) for k, v in best.items()}
    neigh = _neighbours(best, grid)
    within = []
    for n in neigh:
        mask = np.logical_and.reduce([surf[k] == v for k, v in n.items()])
        sv = float(surf[mask]["sharpe"].iloc[0])
        within.append(abs(sv - float(best_row["sharpe"])) <= 0.30 * abs(float(best_row["sharpe"])) if best_row["sharpe"] != 0 else False)
    rep.surface_summary = {"median_sharpe": float(surf["sharpe"].median()), "best": best, "best_sharpe": float(best_row["sharpe"]),
                           "neighbours": len(neigh), "neighbours_within_30pct": float(np.mean(within)) if within else float("nan"),
                           "positive_share": float((surf["sharpe"] > 0).mean())}
    # ---- validation on C with the pre-declared default parameters (never the B-best)
    val = run_trial(module_id, base_params, "C", data_c, registry=registry, **run_kw)
    res = val.result
    pnls = np.array([t.pnl for t in res.trades], dtype=float)
    lo, mean, hi = block_bootstrap_ci(pnls, n_boot=n_boot) if len(pnls) else (float("nan"),) * 3
    rets = res.equity.pct_change().dropna()
    sk, ku = moments(rets)
    rep.validation = {"params": base_params, "sharpe": val.metrics["sharpe"], "total_return": val.metrics["total_return"], "max_drawdown": val.metrics["max_drawdown"],
                      "trades": int(len(pnls)), "expectancy_per_trade": mean, "expectancy_ci90": (lo, hi), "costs_paid": val.metrics.get("costs_paid", 0.0)}
    rep.attribution = dict(res.attribution.get(module_id, {}))
    # ---- deflated Sharpe: trials on this module and cut C
    n_trials = registry.count(module_id, "C") if registry else 1
    trial_srs = registry.sharpes(module_id, "C") if registry else None
    rep.deflated = deflated_sharpe(val.metrics["sharpe"], n_trials=max(n_trials, 1), n_obs=len(rets), skew=sk, kurt=ku, trial_sharpes=trial_srs)
    # ---- cost stress on C
    srows = []
    for name in STRESSES:
        if name == "base":
            srows.append({"stress": "base", "sharpe": val.metrics["sharpe"], "total_return": val.metrics["total_return"], "trades": int(len(pnls)), "net_profit": val.metrics["net_profit"]})
            continue
        st = run_trial(module_id, base_params, "C", data_c, stress=name, registry=registry, **run_kw)
        srows.append({"stress": name, "sharpe": st.metrics["sharpe"], "total_return": st.metrics["total_return"], "trades": st.metrics["trade_count"], "net_profit": st.metrics["net_profit"]})
    rep.stresses = pd.DataFrame(srows)
    # ---- causal regimes on C
    bench = data_c[benchmark_symbol] if benchmark_symbol in data_c else next(iter(data_c.values()))
    close = bench["close"]
    if isinstance(close.index, pd.DatetimeIndex) and len(close) and (close.index[1] - close.index[0]) < pd.Timedelta(days=1):
        close = close.groupby(close.index.tz_convert("America/New_York").date).last()
        close.index = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d in close.index])
    eq = res.equity.copy()
    eq.index = pd.DatetimeIndex([pd.Timestamp(ts.date(), tz="America/New_York") for ts in eq.index])
    eq = eq[~eq.index.duplicated(keep="last")]
    reg = classify_regimes(close.reindex(close.index.union(eq.index)).ffill()).reindex(eq.index).fillna(False)
    rep.regimes = regime_table({module_id: eq.pct_change().fillna(0.0)}, reg)
    rep.regimes["share"] = rep.regimes["days"] / max(len(eq), 1)
    # ---- criteria
    c = []
    ss = rep.surface_summary
    c.append((CRITERIA[0], bool(ss["median_sharpe"] > 0 and (math.isnan(ss["neighbours_within_30pct"]) or ss["neighbours_within_30pct"] >= 0.70)),
              f"median {ss['median_sharpe']:.2f}, neighbours within 30%: {ss['neighbours_within_30pct']:.0%}" if not math.isnan(ss["neighbours_within_30pct"]) else f"median {ss['median_sharpe']:.2f}, no neighbours"))
    c.append((CRITERIA[1], bool(len(pnls) >= 10 and lo > 0), f"n={len(pnls)} expectancy {mean:+.2f} CI90 [{lo:+.2f}, {hi:+.2f}]" if len(pnls) else "no trades"))
    base_np = float(val.metrics["net_profit"])
    st_ok = all((r["net_profit"] > 0 and (base_np <= 0 or r["net_profit"] >= 0.40 * base_np)) for _, r in rep.stresses.iterrows()) if base_np > 0 else False
    c.append((CRITERIA[2], st_ok, "; ".join(f"{r['stress']} {r['net_profit']:+.0f}" for _, r in rep.stresses.iterrows())))
    bad = rep.regimes[(rep.regimes["sharpe"] < -0.5) & (rep.regimes["share"] > 0.25)]
    c.append((CRITERIA[3], bool(bad.empty), "none" if bad.empty else ", ".join(f"{r['regime']} sharpe {r['sharpe']:.2f} share {r['share']:.0%}" for _, r in bad.iterrows())))
    mech = MECHANISM.get(module_id, "overnight")
    on, intra = rep.attribution.get("overnight", 0.0), rep.attribution.get("intraday", 0.0)
    tot = on + intra
    mech_ok = (tot > 0) and ((intra >= 0.5 * tot) if mech == "intraday" else (on >= 0.5 * tot))
    c.append((CRITERIA[4], bool(mech_ok), f"overnight {on:+.0f} intraday {intra:+.0f} (mechanism: {mech})"))
    rep.criteria = c
    return rep


# ------------------------------------------------------------------------------------------------- reporting
def _md_table(df: pd.DataFrame, floatfmt: str = "{:.3f}") -> str:
    if df is None or df.empty:
        return "_(empty)_"
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(floatfmt.format(v) if isinstance(v, (float, np.floating)) else str(v) for v in r.values) + " |")
    return "\n".join(out)


def render_results(reports: list[ModuleReport], *, registry: TrialRegistry | None = None, generated: datetime | None = None) -> str:
    now = (generated or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    L = [f"# RESULTS V1.5 — research protocol report (generated {now})", "",
         "Generated by `python -m bot research report`. Cuts: A 2016–2018 exploration, B 2019–2021 development, C 2022–2023 validation, "
         "D 2024–2026-06 holdout (sealed; see UNSEAL_LOG.md). Every number below is a backtest through the V1.5 spine (SimBroker fills, "
         "RiskEngine admission, Allocator, OrderManager) with the production throttle disabled. **A pass here is a research result, not "
         "evidence of live profitability; promotion additionally requires the sealed cut D once and a PROMOTIONS.md entry.**", ""]
    for rep in reports:
        L += [f"## {rep.module}", ""]
        if rep.status != "evaluated":
            L += [f"**NOT EVALUATED** — {rep.reason}", ""]
            continue
        verdict = "PASS" if rep.passed else "FAIL"
        L += [f"**Verdict on B+C: {verdict}**  (code {rep.code}; data {rep.data_note})", ""]
        L += ["### Parameter surface on B (declared surface, Sharpe)", "", _md_table(rep.surface), "",
              f"median Sharpe {rep.surface_summary['median_sharpe']:.2f}; best {rep.surface_summary['best']} at {rep.surface_summary['best_sharpe']:.2f}; "
              f"neighbours within 30% of best: {rep.surface_summary['neighbours_within_30pct']:.0%} of {rep.surface_summary['neighbours']}; "
              f"positive share {rep.surface_summary['positive_share']:.0%}", ""]
        v = rep.validation
        lo, hi = v["expectancy_ci90"]
        L += ["### Validation on C (pre-declared default parameters, never the B-best)", "",
              f"params {v['params']}; Sharpe {v['sharpe']:.2f}; return {v['total_return']:+.2%}; max drawdown {v['max_drawdown']:+.2%}; trades {v['trades']}; "
              f"net expectancy per trade {v['expectancy_per_trade']:+.2f} with block-bootstrap 90% interval [{lo:+.2f}, {hi:+.2f}]; costs paid {v['costs_paid']:.2f}", ""]
        d = rep.deflated
        L += [f"Deflated Sharpe: SR {d['sharpe_ann']:.2f} vs expected max under {d['n_trials']} trial(s) on C SR0 {d['sr0_ann']:.2f} → DSR probability {d['dsr']:.2f}", ""]
        L += ["### Cost stress on C", "", _md_table(rep.stresses), ""]
        L += ["### Causal regimes on C (trend = sign of trailing 126-day return; vol = trailing quantile from past data only)", "", _md_table(rep.regimes), ""]
        L += [f"### Attribution: overnight {rep.attribution.get('overnight', 0.0):+.2f}, intraday {rep.attribution.get('intraday', 0.0):+.2f}", ""]
        L += ["### Criteria", ""]
        for text, ok, detail in rep.criteria:
            L.append(f"- {'PASS' if ok else 'FAIL'} — {text} — {detail}")
        L.append("")
    if registry is not None:
        rows = registry.rows()
        if len(rows):
            counts = rows.groupby(["module", "cut"]).agg(trials=("params_hash", "count"), distinct_params=("params_hash", "nunique")).reset_index()
            L += ["## Trial registry", "", _md_table(counts), ""]
    return "\n".join(L)
