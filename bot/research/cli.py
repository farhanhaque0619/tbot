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
    if args.research_cmd in ("run", "report", "trials"):
        return run_protocol(args, console, settings)
    if args.research_cmd == "regimes":
        from bot.research.harness import classify_regimes
        data = _load(settings, symbols, args.start, args.end, 0)
        for s, df in data.items():
            reg = classify_regimes(df["close"])
            console.print(f"{s}: " + ", ".join(f"{c}={int(reg[c].sum())}d" for c in reg.columns))
        return 0
    return 1


# ------------------------------------------------------------------------------------------------ V1.5 protocol
def _parse_params(text: str | None) -> dict:
    out: dict = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        v = v.strip()
        low = v.lower()
        if low in ("true", "false"):
            out[k.strip()] = low == "true"
        else:
            try:
                out[k.strip()] = int(v) if v.lstrip("-").isdigit() else float(v)
            except ValueError:
                out[k.strip()] = v
    return out


def _protocol_data_for(settings, *, symbols_override: list[str] | None):
    """(module, cut) -> bars from the local cache only (daily through the loader, minute from bars_1m); never fetches."""
    from datetime import timedelta

    from bot.data.store import BarStore
    from bot.data.universe import Universe
    from bot.research.protocol import CUTS, MINUTE_MODULES
    store = BarStore(settings.data_db_path)

    def data_for(module: str, cut: str) -> dict[str, pd.DataFrame]:
        start, end = CUTS[cut]
        if symbols_override:
            syms = [s.upper() for s in symbols_override]
        elif module == "M3":
            u = Universe.load(settings.universe_path)
            syms = list(u.tier3) + ["SPY"]
            if not u.tier3:
                raise RuntimeError("universe tier3 is empty (python -m bot universe build)")
        else:
            syms = ["SPY", "QQQ"]
        out: dict[str, pd.DataFrame] = {}
        for s in syms:
            if module in MINUTE_MODULES:
                df = store.get_minute_bars(s, start - timedelta(days=45), end)
                if df.empty:
                    raise RuntimeError(f"{s}: no minute bars for cut {cut} in the cache (python -m bot data fetch --timeframe 1m)")
            else:
                df = store.get_bars(s, start - timedelta(days=760), end)
                if df.empty or df.index[-1].date() < end - timedelta(days=10) or df.index[0].date() > start - timedelta(days=400):
                    raise RuntimeError(f"{s}: daily cache does not cover cut {cut} with 260 sessions of warm-up (python -m bot data fetch)")
            out[s] = df
        return out
    return data_for


def run_protocol(args, console, settings) -> int:
    from bot.research import protocol as P
    registry = P.TrialRegistry()
    data_for = _protocol_data_for(settings, symbols_override=args.symbol)
    if args.research_cmd == "trials":
        rows = registry.rows(module=(args.module or [None])[0])
        if rows.empty:
            console.print("no trials registered")
            return 0
        counts = rows.groupby(["module", "cut"]).agg(trials=("params_hash", "count"), distinct_params=("params_hash", "nunique"),
                                                     best_sharpe=("sharpe", "max"), median_sharpe=("sharpe", "median")).reset_index()
        console.print(counts.to_string(index=False))
        return 0
    if args.research_cmd == "run":
        if not args.module or not args.cut:
            raise SystemExit("research run needs --module and --cut")
        for mod in args.module:
            params = {**P.DEFAULT_PARAMS.get(mod, {}), **_parse_params(args.params)}
            try:
                P.check_cut_access(args.cut, unseal=False) if args.cut in P.SEALED_CUTS and not args.unseal else None   # seal first, data second
            except P.SealedHoldoutError as e:
                console.print(f"[red]{e}[/red]")
                return 3
            try:
                data = data_for(mod, args.cut)
            except RuntimeError as e:
                console.print(f"[red]{mod}: {e}[/red]")
                return 2
            try:
                tr = P.run_trial(mod, params, args.cut, data, registry=registry, stress=args.stress, fractional=args.fractional,
                                 unseal=args.unseal, reason=args.reason, initial_cash=args.cash)
            except P.SealedHoldoutError as e:
                console.print(f"[red]{e}[/red]")
                return 3
            m = tr.metrics
            console.print(f"{mod} cut {args.cut} ({P.CUT_ROLE[args.cut]}) stress={args.stress} params={params} -> trial #{tr.trial_id}")
            console.print(f"  return {m['total_return']:+.2%}  sharpe {m['sharpe']:.2f}  maxDD {m['max_drawdown']:+.2%}  trades {m['trade_count']}  "
                          f"orders {m.get('orders', 0)}  costs {m.get('costs_paid', 0.0):.2f}  attribution {m['attribution']}"
                          + ("  KILL SWITCH" if m.get("kill_switch") else ""))
            console.print(f"  trials on {mod}/{args.cut} so far: {registry.count(mod, args.cut)} distinct parameter sets")
        return 0
    if args.research_cmd == "report":
        mods = args.module or ["M1", "M2", "M3"]
        reports = []
        for mod in mods:
            console.print(f"[dim]evaluating {mod} (surface on B, validation/stress/regimes on C)...[/dim]")
            reports.append(P.evaluate_module(mod, data_for, registry=registry, n_boot=args.n_boot, fractional=args.fractional, initial_cash=args.cash))
            r = reports[-1]
            console.print(f"{mod}: {r.status}" + (f" — {r.reason}" if r.reason else f" — {'PASS' if r.passed else 'FAIL'}"))
        md = P.render_results(reports, registry=registry)
        out = P.RESEARCH_DIR / "RESULTS_V1_5.md"
        out.write_text(md, encoding="utf-8")
        console.print(f"[dim]wrote {out}[/dim]")
        return 0
    return 1
