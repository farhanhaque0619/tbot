"""Post-session review (Phase 15). Reads logs and state, writes a markdown report with PROPOSALS.
It never changes configuration, thresholds, code, or state, and never trades."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from bot.config import Settings


def _load_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"corrupt": True})
    return out


def build_review(settings: Settings, run_id: str = "paper") -> str:
    from bot.execution.state import StateStore

    state = StateStore(settings.state_dir / f"{run_id}.json").load()
    decisions = _load_jsonl(settings.log_dir / f"decisions_{run_id}.jsonl")
    shadow = _load_jsonl(settings.log_dir / f"advisor_shadow_{run_id}.jsonl")
    L = [f"# Session review — run `{run_id}` ({state.env or 'unknown env'}) — generated {datetime.now():%Y-%m-%d %H:%M}", ""]
    L.append("_This report proposes. It changed nothing. Any change needs: tests pass, backtests pass, diff reviewed, operator approves._")
    L.append("")
    # --- decisions
    by = {}
    for d in decisions:
        by.setdefault(d.get("order_decision", "?"), []).append(d)
    L += ["## Decisions", "", "| outcome | count |", "|---|---|"] + [f"| {k} | {len(v)} |" for k, v in sorted(by.items())]
    blocked = [d for d in decisions if d.get("order_decision") == "blocked"]
    if blocked:
        codes = {}
        for d in blocked:
            code = (d.get("risk_decision") or {}).get("code") or (d.get("notes") or ["?"])[-1]
            codes[code] = codes.get(code, 0) + 1
        L += ["", "Blocked by reason:", ""] + [f"- `{c}`: {n}" for c, n in sorted(codes.items(), key=lambda x: -x[1])]
    missed = [d for d in decisions if d.get("signal") and d.get("desired_position") not in (None, d.get("current_broker_position")) and d.get("order_decision") in ("blocked", "waiting", "skip")]
    L += ["", f"Missed signals (desired ≠ actual but no order): {len(missed)}"]
    for d in missed[-10:]:
        L.append(f"- {d.get('session')} {d.get('symbol')}: target {d['signal'].get('target')} ({d['signal'].get('reason')}) → {d.get('order_decision')} {(d.get('risk_decision') or {}).get('code', '')} {'; '.join(d.get('notes') or [])}")
    # --- fills & slippage
    fills = [d for d in decisions if d.get("order_decision") == "fill"]
    slips = [d["realized_slippage_bps"] for d in fills if d.get("realized_slippage_bps") is not None]
    L += ["", "## Fills", "", f"fills: {len(fills)}"]
    if slips:
        expected = settings.slippage_bps + settings.spread_bps / 2
        L += [f"realized slippage vs reference: mean {mean(slips):+.1f} bps, min {min(slips):+.1f}, max {max(slips):+.1f} (n={len(slips)}); "
              f"backtest assumption {expected:.1f} bps per side"]
        if mean(slips) > expected * 1.5 and len(slips) >= 5:
            L.append(f"- PROPOSAL: realized slippage exceeds the backtest assumption by >50%; re-run backtests with SLIPPAGE_BPS≈{mean(slips):.0f} before trusting them.")
    # --- P&L and risk
    eq = state.equity_log
    if len(eq) >= 2:
        first, last = eq[0]["equity"], eq[-1]["equity"]
        today = [e for e in eq if e["ts"][:10] == eq[-1]["ts"][:10]]
        L += ["", "## P&L", "", f"equity {first:,.2f} → {last:,.2f} over {len(eq)} marks; "
              f"today {today[0]['equity']:,.2f} → {today[-1]['equity']:,.2f} ({today[-1]['equity'] - today[0]['equity']:+,.2f})"]
    r = state.risk or {}
    L += ["", "## Risk", "", f"peak {r.get('peak_equity', 0):,.2f} · day start {r.get('day_start_equity', 0):,.2f} · halted_today={r.get('halted_today')} · killed={r.get('killed')} {r.get('kill_reason', '')}",
          f"closed trades: {len(state.trades)} · open positions: {list(state.positions)} · last error: {state.last_error or 'none'}"]
    if r.get("killed"):
        L.append("- ACTION REQUIRED (human): kill switch is engaged. Review the cause before `python -m bot risk reset`.")
    # --- advisor calibration
    if shadow:
        L += ["", "## Advisor shadow mode", "", f"{len(shadow)} advice records. Run `python -m bot research regimes` with the shadow file to compute calibration; "
              "no threshold is trusted until n is large and buckets separate."]
    # --- V1.5 execution store (daemon runs)
    L += v15_section(settings, run_id)
    # --- proposals
    L += ["", "## Proposals (require operator review; nothing was changed)", ""]
    props = []
    if not decisions:
        props.append("No decisions logged yet: run at least one `--once` cycle around a session close.")
    waiting = len(by.get("waiting", []))
    if waiting > 3 * max(len(by.get("submit", [])), 1):
        props.append("Many `waiting` decisions: check that the process runs inside the order window (19:00–09:28 ET for OPG, market hours for fractional).")
    if any((d.get("risk_decision") or {}).get("code") == "data_fresh" for d in blocked):
        props.append("Orders were blocked on data freshness: verify DATA_FEED/quote availability on your plan.")
    if state.trades and len(state.trades) >= 10:
        pnls = [t["pnl"] for t in state.trades]
        props.append(f"{len(pnls)} closed trades, win rate {sum(p > 0 for p in pnls) / len(pnls):.0%}, net {sum(pnls):+,.2f}: still far too few to update any belief about edge.")
    props.append("Strategy-parameter changes must go through `python -m bot research surface` and a walk-forward run; never edit live parameters from this report.")
    L += [f"- {p}" for p in props]
    return "\n".join(L) + "\n"


def write_review(settings: Settings, run_id: str = "paper", out_dir: Path = Path("reports")) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"review_{run_id}_{datetime.now():%Y%m%d_%H%M}.md"
    p.write_text(build_review(settings, run_id), encoding="utf-8")
    return p


def v15_section(settings: Settings, run_id: str) -> list[str]:
    """Per-module expectancy, slippage vs assumption, attribution, throttles, unprotected positions from state/<run_id>.sqlite."""
    db = settings.state_dir / f"{run_id}.sqlite"
    if not db.exists():
        return []
    from bot.execution.store import ExecutionStore
    es = ExecutionStore(db)
    L = ["", f"## V1.5 daemon store (`{db}`)", ""]
    trades = es.trades()
    by_mod: dict[str, list[float]] = {}
    for t in trades:
        by_mod.setdefault(t["module"], []).append(float(t["pnl"] or 0.0))
    if by_mod:
        L += ["| module | trades | expectancy/trade | win rate | net |", "|---|---|---|---|---|"]
        for m, p in sorted(by_mod.items()):
            L.append(f"| {m} | {len(p)} | {mean(p):+.2f} | {sum(x > 0 for x in p) / len(p):.0%} | {sum(p):+.2f} |")
    else:
        L.append("no closed trades in the store yet")
    ref = {o["client_order_id"]: (o["reference_price"], o["side"], o["module"]) for o in es.orders() if o.get("reference_price")}
    slips: dict[str, list[float]] = {}
    for f in es.fills():
        if f["event"] in ("fill", "partial_fill") and f.get("price") and f["client_order_id"] in ref and ref[f["client_order_id"]][0]:
            rp, side, m = ref[f["client_order_id"]]
            slips.setdefault(m, []).append((f["price"] - rp) / rp * 1e4 * (1 if side == "buy" else -1))
    expected = settings.slippage_bps + settings.spread_bps / 2
    if slips:
        L += ["", f"realised slippage vs reference (backtest assumption {expected:.1f} bps per side):"]
        L += [f"- {m}: mean {mean(v):+.1f} bps over {len(v)} fills" + (" ← exceeds the assumption by more than 3 bps" if mean(v) - expected > 3 else "") for m, v in sorted(slips.items())]
    thr = es.throttles()
    L += ["", "throttles: " + (", ".join(f"{m} x{x:.2f} ({r})" for m, (x, r) in thr.items()) if thr else "none")]
    unp = [p for p in es.positions() if p.get("unprotected") or p.get("unprotected_overnight")]
    L += ["unprotected positions: " + (", ".join(f"{p['symbol']}({p['module']}{', overnight' if p.get('unprotected_overnight') else ''})" for p in unp) if unp else "none")]
    orphans = [p for p in es.positions() if p["module"] == "orphan"]
    if orphans:
        L.append("orphan positions adopted by reconciliation: " + ", ".join(f"{p['symbol']} {p['qty']:g}" for p in orphans))
    recon = es.decisions(kind="reconciliation", limit=50)
    L.append(f"reconciliation records (last 50): {len(recon)}" + (f"; latest: {recon[0].get('kind')} {recon[0].get('symbol', '')}" if recon else ""))
    wd = [c for c in es.heartbeats() if c.startswith("watchdog:")]
    L.append("watchdog actions ever taken: " + (", ".join(sorted(c.split(':', 1)[1] for c in wd)) if wd else "none"))
    hb = es.heartbeats().get("daemon")
    L.append(f"daemon heartbeat: {hb[0]} ({hb[1]})" if hb else "daemon heartbeat: never")
    return L
