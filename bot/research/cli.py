"""`python -m bot research ...` (brain layer; never orders)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from bot.backtest.costs import CostModel
from bot.config import get_settings
from bot.risk.manager import RiskLimits
from bot.strategies import STRATEGIES


def _all_strategies():
    from bot.research.candidates import RESEARCH_STRATEGIES
    return {**STRATEGIES, **RESEARCH_STRATEGIES}


def _load(settings, symbols, start, end, warmup):
    from bot.cli import _make_loader
    _, loader = _make_loader(settings)
    data = {}
    for s in symbols:
        df = loader.get_daily(s, start or date(1900, 1, 1), end or date.today(), warmup=warmup)
        if df.empty:
            raise SystemExit(f"{s}: no bars")
        data[s.upper()] = df
    return data


def run_research(args, console) -> int:
    settings = get_settings()
    costs, risk = CostModel.from_settings(settings), RiskLimits.from_settings(settings)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    strategies = _all_strategies()
    if args.research_cmd == "hypotheses":
        p = Path("research/HYPOTHESES.md")
        console.print(p.read_text() if p.exists() else "research/HYPOTHESES.md not found")
        return 0
    symbols = args.symbol or ["SPY"]
    ts = lambda d: pd.Timestamp(d, tz="America/New_York") if d else None  # noqa: E731
    tag = "_".join(s.upper() for s in symbols) + (f"_{args.start}" if args.start else "") + (f"_{args.end}" if args.end else "")
    if args.research_cmd == "compare":
        from bot.research.harness import compare
        warmup = max(c(**{}).warmup for c in strategies.values()) + 20
        data = _load(settings, symbols, args.start, args.end, warmup)
        names = [args.strategy] if args.strategy else list(strategies)
        res = compare({n: (lambda c=strategies[n]: c()) for n in names}, data, costs=costs, risk=risk, cash=args.cash,
                      start=ts(args.start), end=ts(args.end) + pd.Timedelta(hours=23) if args.end else None,
                      walk_forward_classes={n: strategies[n] for n in names})
        md = f"# Research comparison · {', '.join(data)}\n" + res.render_markdown()
        p = out_dir / f"research_compare_{tag}.md"
        p.write_text(md, encoding="utf-8")
        console.print(md)
        console.print(f"[dim]wrote {p}[/dim]")
        return 0
    if args.research_cmd == "surface":
        from bot.research.surface import parameter_surface
        if not args.strategy:
            raise SystemExit("--strategy required")
        cls = strategies[args.strategy]
        warmup = max(cls(**p).warmup for p in cls.param_grid()) + 20
        data = _load(settings, symbols, args.start, args.end, warmup)
        sr = parameter_surface(cls, data, costs=costs, risk=risk, cash=args.cash, start=ts(args.start), end=ts(args.end) + pd.Timedelta(hours=23) if args.end else None)
        md = f"# Parameter surface · {cls.name} · {', '.join(data)}\n\n" + sr.render_markdown() + "\n\n" + "\n".join(f"_{n}_" for n in sr.notes)
        p = out_dir / f"research_surface_{cls.name}_{tag}.md"
        p.write_text(md, encoding="utf-8")
        console.print(md)
        console.print(f"[dim]wrote {p}[/dim]")
        return 0
    if args.research_cmd == "regimes":
        from bot.research.harness import classify_regimes
        data = _load(settings, symbols, args.start, args.end, 0)
        for s, df in data.items():
            reg = classify_regimes(df["close"])
            console.print(f"{s}: " + ", ".join(f"{c}={int(reg[c].sum())}d" for c in reg.columns))
        return 0
    return 1
