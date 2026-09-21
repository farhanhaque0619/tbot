"""CLI dashboard: equity curve (sparkline), risk status, open positions, recent trades, recent orders."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from bot.execution.state import StateStore

BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 60) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return "(not enough data)"
    if len(vals) > width:  # downsample by taking evenly spaced points
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return BLOCKS[0] * len(vals)
    return "".join(BLOCKS[int((v - lo) / (hi - lo) * (len(BLOCKS) - 1))] for v in vals)


def render(state_path: Path) -> Group:
    state = StateStore(state_path).load()
    risk = state.risk or {}
    eq = [e["equity"] for e in state.equity_log]
    last_eq = eq[-1] if eq else 0.0
    peak = risk.get("peak_equity") or (max(eq) if eq else 0.0)
    dd = (1 - last_eq / peak) if peak else 0.0
    status = "🔴 KILLED" if risk.get("killed") else ("🟡 HALTED TODAY" if risk.get("halted_today") else "🟢 trading")
    head = Table.grid(padding=(0, 2))
    head.add_column(style="bold"); head.add_column()
    head.add_row("Run", f"{state.run_id}  ·  {state.strategy} {state.params}  ·  {', '.join(state.symbols)}")
    head.add_row("Status", f"{status}" + (f"  ({risk.get('kill_reason')})" if risk.get("killed") else ""))
    head.add_row("Equity", f"{last_eq:,.2f}   peak {peak:,.2f}   drawdown {dd:.2%}   day start {risk.get('day_start_equity', 0):,.2f}")
    head.add_row("Last cycle", f"{state.last_cycle or '-'}" + (f"   [red]last error: {state.last_error}[/red]" if state.last_error else ""))
    head.add_row("Equity curve", sparkline(eq))

    pos = Table(title="Open positions", expand=True)
    for c in ("Symbol", "Side", "Qty", "Entry", "Stop", "Since", "Reason"):
        pos.add_column(c)
    for s, p in sorted(state.positions.items()):
        pos.add_row(s, "LONG" if p.get("side", 1) == 1 else "SHORT", str(p.get("qty")),
                    f"{p['entry_price']:.2f}" if p.get("entry_price") else "(pending fill)",
                    f"{p['stop']:.2f}" if p.get("stop") else "-", str(p.get("entry_session") or "-"), str(p.get("reason", ""))[:40])
    if not state.positions:
        pos.add_row("-", "", "", "", "", "", "flat")

    tr = Table(title="Recent trades (closed)", expand=True)
    for c in ("Symbol", "Side", "Qty", "Entry", "Exit", "P&L", "Entry date", "Exit date", "Exit reason"):
        tr.add_column(c)
    for t in state.trades[-10:][::-1]:
        pnl = t.get("pnl", 0.0)
        tr.add_row(t["symbol"], "LONG" if t.get("side", 1) == 1 else "SHORT", str(t["qty"]), f"{t['entry_price']:.2f}",
                   f"{t['exit_price']:.2f}", f"[{'green' if pnl >= 0 else 'red'}]{pnl:+,.2f}[/]",
                   str(t.get("entry_session") or "-"), str(t.get("exit_session") or "-"), str(t.get("exit_reason", ""))[:30])
    if not state.trades:
        tr.add_row("-", "", "", "", "", "", "", "", "none yet")

    od = Table(title="Recent orders", expand=True)
    for c in ("Client order id", "Side", "Qty", "Status", "Fill", "TIF"):
        od.add_column(c)
    for rec in list(state.orders.values())[-8:][::-1]:
        od.add_row(rec["client_order_id"], rec["side"], str(rec["qty"]), rec.get("status", ""),
                   f"{rec['filled_avg_price']:.2f}" if rec.get("filled_avg_price") else "-", rec.get("tif", ""))
    if not state.orders:
        od.add_row("-", "", "", "", "", "")
    return Group(Panel(head, title=f"tbot dashboard  ·  {datetime.now():%Y-%m-%d %H:%M:%S}"), pos, tr, od)


def show(state_path: Path, watch: int | None = None) -> None:
    console = Console()
    if not watch:
        console.print(render(state_path))
        return
    with Live(render(state_path), console=console, refresh_per_second=1) as live:
        while True:
            time.sleep(watch)
            live.update(render(state_path))
