"""Paper -> live candidate gates and live step-up gates (spec §14), computed from the ExecutionStore. Read-only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Any

WATCHDOG_ACTIONS = ("restart_daemon", "halt_entries", "flatten_all", "alert_operator")


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool | None            # None = unknown (no data yet)
    detail: str

    @property
    def label(self) -> str:
        return "PASS" if self.ok else ("UNKNOWN" if self.ok is None else "FAIL")


def paper_gates(store, policy, *, backtest_slippage_bps: float = 3.0, backtest_m2_expectancy_sign: int | None = None,
                promotions_path=None) -> list[GateResult]:
    out: list[GateResult] = []
    # 1. >= 60 autonomous sessions
    sessions = {row[1][:10] for row in store.heartbeat_log("daemon_session")}
    out.append(GateResult("sessions_autonomous_ge_60", (len(sessions) >= 60) if sessions else None, f"{len(sessions)} sessions with a daemon heartbeat"))
    # 2. zero unreconciled discrepancies, orphans, duplicates
    recon = [d for d in store.decisions(kind="reconciliation", limit=100000)]
    unresolved = [d for d in recon if d.get("kind") in ("position_mismatch", "orphan_flatten_failed", "unknown_order_cancel_failed", "position_missing_at_broker")]
    orphans = [p for p in store.positions() if p["module"] == "orphan"]
    dups = [d for d in store.decisions(kind="duplicate", limit=100000)]
    out.append(GateResult("zero_discrepancies_orphans_duplicates", not (unresolved or orphans or dups) if sessions else None,
                          f"unresolved discrepancies {len(unresolved)}, orphan positions {len(orphans)}, duplicates {len(dups)}"))
    # 3. realised slippage within 3 bps of the backtest assumption
    slips = []
    ref = {o["client_order_id"]: (o["reference_price"], o["side"]) for o in store.orders() if o.get("reference_price")}
    for f in store.fills():
        if f["event"] not in ("fill", "partial_fill") or not f.get("price") or f["client_order_id"] not in ref:
            continue
        rp, side = ref[f["client_order_id"]]
        if rp and rp > 0:
            slips.append((f["price"] - rp) / rp * 1e4 * (1 if side == "buy" else -1))
    if slips:
        realised = mean(slips)
        out.append(GateResult("slippage_within_3bps_of_backtest", abs(realised - backtest_slippage_bps) <= 3.0,
                              f"realised {realised:+.1f} bps vs assumed {backtest_slippage_bps:.1f} bps over {len(slips)} fills"))
    else:
        out.append(GateResult("slippage_within_3bps_of_backtest", None, "no fills recorded"))
    # 4. each watchdog action exercised at least once
    seen = {c.split(":", 1)[1] for c in store.heartbeats() if c.startswith("watchdog:")}
    missing = [a for a in WATCHDOG_ACTIONS if a not in seen]
    out.append(GateResult("watchdog_actions_exercised", not missing if sessions else None, "all exercised" if not missing else f"never exercised: {missing}"))
    # 5. M2 >= 120 paper trades with net expectancy of the same sign as the backtest
    m2 = store.trades("M2")
    if m2:
        exp = mean(t["pnl"] for t in m2)
        same_sign = None if backtest_m2_expectancy_sign is None else ((exp > 0) == (backtest_m2_expectancy_sign > 0))
        ok = len(m2) >= 120 and (same_sign is not False)
        out.append(GateResult("m2_120_trades_same_sign_expectancy", ok if same_sign is not None or len(m2) < 120 else None,
                              f"{len(m2)} M2 trades, expectancy {exp:+.2f}/trade" + ("" if backtest_m2_expectancy_sign is not None else " (backtest sign unknown)")))
    else:
        out.append(GateResult("m2_120_trades_same_sign_expectancy", None if "M2" in policy.allowed_modules else True,
                              "no M2 trades" if "M2" in policy.allowed_modules else "M2 not in the policy"))
    # 6. promotion record for every module in the live policy
    from bot.core.policy import PROMOTIONS_PATH
    bad = policy.unpromoted_modules(promotions_path or PROMOTIONS_PATH)
    out.append(GateResult("promotion_record_for_live_modules", not bad, "all promoted" if not bad else f"unpromoted: {bad}"))
    return out


def live_gates(store, *, safe_mode_sessions: int, last_stepup: date | None, paper_slippage_bps: float | None) -> list[GateResult]:
    out: list[GateResult] = []
    sessions = {row[1][:10] for row in store.heartbeat_log("daemon_session")}
    out.append(GateResult("live_sessions_at_safe_caps_ge_20", (len(sessions) >= 20) if sessions else None, f"{len(sessions)} live sessions"))
    if last_stepup is not None:
        out.append(GateResult("stepup_at_most_2x_per_month", (date.today() - last_stepup).days >= 30, f"last step-up {last_stepup}"))
    else:
        out.append(GateResult("stepup_at_most_2x_per_month", True, "no step-up yet"))
    slips = []
    ref = {o["client_order_id"]: (o["reference_price"], o["side"]) for o in store.orders() if o.get("reference_price")}
    for f in store.fills():
        if f["event"] in ("fill", "partial_fill") and f.get("price") and f["client_order_id"] in ref and ref[f["client_order_id"]][0]:
            rp, side = ref[f["client_order_id"]]
            slips.append((f["price"] - rp) / rp * 1e4 * (1 if side == "buy" else -1))
    if slips and paper_slippage_bps is not None:
        realised = mean(slips)
        out.append(GateResult("slippage_degradation_lt_5bps_vs_paper", realised - paper_slippage_bps < 5.0, f"live {realised:+.1f} bps vs paper {paper_slippage_bps:+.1f} bps"))
    else:
        out.append(GateResult("slippage_degradation_lt_5bps_vs_paper", None, "no live fills or no paper reference"))
    return out


def render(gates: list[GateResult]) -> list[tuple[str, str, str]]:
    return [(g.name, g.label, g.detail) for g in gates]


def summary(gates: list[GateResult]) -> dict[str, Any]:
    return {"pass": sum(g.ok is True for g in gates), "fail": sum(g.ok is False for g in gates), "unknown": sum(g.ok is None for g in gates), "all_pass": all(g.ok is True for g in gates)}
