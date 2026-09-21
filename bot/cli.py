"""Command line interface: python -m bot <command> ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console

from bot import __version__
from bot.config import Settings, get_settings
from bot.monitoring.logging import setup_logging

log = logging.getLogger("bot.cli")
console = Console()


# ----------------------------------------------------------------------------- helpers
def _parse_params(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--param expects key=value, got {it!r}")
        k, v = it.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _make_loader(settings: Settings, *, need_provider: bool = False):
    from bot.data.loader import BarLoader
    from bot.data.store import BarStore

    store = BarStore(settings.data_db_path)
    provider = None
    if settings.has_alpaca_keys:
        from bot.data.providers import AlpacaBarProvider
        provider = AlpacaBarProvider(settings)
    elif need_provider:
        raise SystemExit("Alpaca keys are not configured (copy .env.example to .env). "
                         "Offline: import CSV bars with `python -m bot data import`.")
    return store, BarLoader(store, provider, adjustment=settings.data_adjustment)


def _confirm_live(args, settings: Settings) -> bool:
    """Three independent gates before real money: env flag, CLI flag, typed confirmation."""
    if not settings.live_trading:
        return False
    if not getattr(args, "i_understand_live_trading", False):
        raise SystemExit("LIVE_TRADING=true in the environment but --i-understand-live-trading was not passed. Refusing.")
    console.print("[bold red]LIVE TRADING requested. This will place REAL orders with REAL money.[/bold red]")
    if not sys.stdin.isatty():
        raise SystemExit("Live trading needs an interactive terminal for confirmation. Refusing.")
    typed = input("Type LIVE to continue, anything else to abort: ").strip()
    if typed != "LIVE":
        raise SystemExit("Aborted. (Paper trading is the default; unset LIVE_TRADING.)")
    return True


# ---------------------------------------------------------------------------- commands
def cmd_backtest(args) -> int:
    from bot.backtest import Backtester, CostModel, walk_forward
    from bot.backtest.report import print_backtest, print_walkforward, save_backtest, save_walkforward
    from bot.risk import RiskLimits
    from bot.strategies import get_strategy_class

    settings = get_settings()
    strategy_cls = get_strategy_class(args.strategy)
    params = _parse_params(args.param)
    grid = strategy_cls.param_grid()
    warmup = max(strategy_cls(**p).warmup for p in grid + [params]) + 20
    _, loader = _make_loader(settings)
    data = {}
    for sym in args.symbol:
        df = loader.get_daily(sym, args.start, args.end, warmup=warmup, refresh=args.refresh)
        if df.empty:
            raise SystemExit(f"{sym}: no bars for {args.start}..{args.end}")
        data[sym.upper()] = df
        console.print(f"[dim]{sym.upper()}: {len(df)} bars {df.index[0].date()} → {df.index[-1].date()} (incl. {warmup} warm-up)[/dim]")
    costs = CostModel.from_settings(settings)
    risk = RiskLimits.from_settings(settings)
    console.print(f"[dim]costs: slippage {costs.slippage_bps}bps + half-spread {costs.spread_bps / 2}bps per side "
                  f"(round trip {costs.round_trip_bps():.1f}bps); risk/trade {risk.risk_per_trade_pct:.1%}, "
                  f"max position {risk.max_position_pct:.0%}, kill switch {risk.max_drawdown_pct:.0%} DD[/dim]")
    ts_start, ts_end = pd.Timestamp(args.start, tz="America/New_York"), pd.Timestamp(args.end, tz="America/New_York") + pd.Timedelta(hours=23)
    tag = f"{args.strategy}_{'_'.join(s.upper() for s in args.symbol)}_{args.start}_{args.end}"
    out_dir = Path(args.out)

    bt = Backtester(lambda: strategy_cls(**params), initial_cash=args.cash, costs=costs, risk=risk,
                    trade_start=ts_start, trade_end=ts_end)
    res = bt.run(data)
    print_backtest(res, console)
    if not args.no_save:
        for p in save_backtest(res, out_dir, tag):
            console.print(f"[dim]wrote {p}[/dim]")

    if not args.no_walk_forward:
        try:
            wf = walk_forward(strategy_cls, data, train_years=args.train_years, test_years=args.test_years,
                              initial_cash=args.cash, costs=costs, risk=risk, start=ts_start, end=ts_end,
                              min_trades=args.min_trades)
        except ValueError as e:
            console.print(f"[yellow]walk-forward skipped: {e}[/yellow]")
            return 0
        print_walkforward(wf, console)
        if not args.no_save:
            for p in save_walkforward(wf, out_dir, tag):
                console.print(f"[dim]wrote {p}[/dim]")
    return 0


def cmd_paper(args) -> int:
    from bot.execution.broker import AlpacaBroker
    from bot.execution.paper_loop import PaperTrader
    from bot.execution.state import StateStore
    from bot.monitoring.alerts import Alerter
    from bot.risk import RiskLimits
    from bot.strategies import get_strategy_class

    settings = get_settings()
    live = _confirm_live(args, settings)
    paper = not live
    mode = "LIVE" if live else "paper"
    strategy_cls = get_strategy_class(args.strategy)
    params = _parse_params(args.param)
    strategy_cls(**params)  # validate params early
    store, loader = _make_loader(settings, need_provider=True)
    broker = AlpacaBroker(settings, paper=paper)
    acct = broker.get_account()
    console.print(f"[bold]{mode} trading[/bold] · {strategy_cls.name} {params} · {args.symbol} · "
                  f"account equity {acct.equity:,.2f} cash {acct.cash:,.2f} · broker paper={broker.is_paper}")
    run_id = args.run_id or ("live" if live else "paper")
    trader = PaperTrader(settings=settings, broker=broker, loader=loader, bar_store=store, strategy_cls=strategy_cls,
                         params=params, symbols=args.symbol, state_store=StateStore(settings.state_dir / f"{run_id}.json"),
                         alerter=Alerter(settings.discord_webhook_url.get_secret_value()),
                         risk_limits=RiskLimits.from_settings(settings), run_id=run_id)
    if args.once:
        summary = trader.run_cycle()
        console.print_json(json.dumps(summary, default=str))
        return 0
    trader.run_forever(args.poll)
    return 0


def cmd_dashboard(args) -> int:
    from bot.monitoring.dashboard import show

    settings = get_settings()
    show(settings.state_dir / f"{args.run_id}.json", watch=args.watch)
    return 0


def cmd_status(args) -> int:
    from bot.execution.broker import AlpacaBroker

    settings = get_settings()
    broker = AlpacaBroker(settings, paper=True)
    a = broker.get_account()
    console.print(f"paper account: equity {a.equity:,.2f}  cash {a.cash:,.2f}  buying power {a.buying_power:,.2f}")
    for s, p in broker.get_positions().items():
        console.print(f"  {s}: {p.qty:+d} @ {p.avg_entry_price:.2f} (now {p.current_price:.2f}, value {p.market_value:,.2f})")
    for o in broker.get_open_orders():
        console.print(f"  open order {o.client_order_id}: {o.side} {o.qty} {o.symbol} [{o.status}]")
    c = broker.get_clock()
    console.print(f"market {'OPEN' if c.is_open else 'closed'} · next open {c.next_open} · next close {c.next_close}")
    return 0


def cmd_risk(args) -> int:
    from bot.execution.state import StateStore

    settings = get_settings()
    store = StateStore(settings.state_dir / f"{args.run_id}.json")
    state = store.load()
    if args.risk_cmd == "show":
        console.print_json(json.dumps(state.risk or {}, default=str))
    elif args.risk_cmd == "reset":
        if not state.risk.get("killed"):
            console.print("kill switch is not active")
            return 0
        console.print(f"[red]kill switch reason: {state.risk.get('kill_reason')}[/red]")
        if input("Type RESET to clear the kill switch: ").strip() != "RESET":
            raise SystemExit("aborted")
        state.risk["killed"] = False
        state.risk["kill_reason"] = ""
        state.risk["peak_equity"] = state.risk.get("last_equity", 0.0)
        store.save(state)
        console.print("kill switch cleared; peak equity reset to current equity")
    return 0


def cmd_data(args) -> int:
    settings = get_settings()
    if args.data_cmd == "fetch":
        _, loader = _make_loader(settings, need_provider=True)
        for sym in args.symbol:
            df = loader.get_daily(sym, args.start, args.end, refresh=args.refresh)
            console.print(f"{sym.upper()}: {len(df)} bars cached ({df.index[0].date() if len(df) else '-'} → {df.index[-1].date() if len(df) else '-'})")
    elif args.data_cmd == "import":
        from bot.data.loader import BarLoader
        from bot.data.providers import CsvBarProvider
        from bot.data.store import BarStore

        store = BarStore(settings.data_db_path)
        prov = CsvBarProvider(args.csv, close_column=args.close_column)
        df = prov.fetch_daily(args.symbol, date(1900, 1, 1), date(2100, 1, 1))
        if df.empty:
            raise SystemExit("no rows parsed")
        store.delete_symbol(args.symbol, adjustment=settings.data_adjustment)
        n = store.upsert_bars(args.symbol, df, adjustment=settings.data_adjustment, source=f"csv:{Path(args.csv).name}")
        store.set_coverage(args.symbol, df.index[0].date(), df.index[-1].date(), adjustment=settings.data_adjustment)
        note = " (close-only: open/high/low set equal to close)" if prov.synthetic_ohlc else ""
        console.print(f"imported {n} bars for {args.symbol.upper()} {df.index[0].date()} → {df.index[-1].date()}{note}")
    elif args.data_cmd == "list":
        from bot.data.store import BarStore

        console.print(BarStore(settings.data_db_path).summary().to_string(index=False))
    return 0


# ------------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bot", description=f"tbot {__version__}: backtest + paper-trade strategies on Alpaca")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="run a backtest (+ walk-forward) on cached/fetched daily bars")
    b.add_argument("--strategy", required=True)
    b.add_argument("--symbol", required=True, action="append", help="repeatable")
    b.add_argument("--start", required=True, type=_date)
    b.add_argument("--end", required=True, type=_date)
    b.add_argument("--param", action="append", help="strategy param key=value (repeatable)")
    b.add_argument("--cash", type=float, default=100_000.0)
    b.add_argument("--no-walk-forward", action="store_true")
    b.add_argument("--train-years", type=float, default=3.0)
    b.add_argument("--test-years", type=float, default=1.0)
    b.add_argument("--min-trades", type=int, default=5)
    b.add_argument("--refresh", action="store_true", help="discard cached bars for the symbol(s) and refetch")
    b.add_argument("--out", default="reports")
    b.add_argument("--no-save", action="store_true")
    b.set_defaults(fn=cmd_backtest)

    t = sub.add_parser("paper", help="paper-trade a strategy on Alpaca (default mode)")
    t.add_argument("--strategy", required=True)
    t.add_argument("--symbol", required=True, action="append")
    t.add_argument("--param", action="append")
    t.add_argument("--once", action="store_true", help="run one cycle and exit (cron-friendly)")
    t.add_argument("--poll", type=int, default=None, help="seconds between cycles (default POLL_INTERVAL_SECONDS)")
    t.add_argument("--run-id", default=None, help="state/log namespace (default: paper)")
    t.add_argument("--i-understand-live-trading", action="store_true", help="required (with LIVE_TRADING=true) for real money")
    t.set_defaults(fn=cmd_paper)

    d = sub.add_parser("dashboard", help="show equity, positions, trades from the state file")
    d.add_argument("--run-id", default="paper")
    d.add_argument("--watch", type=int, default=None, help="refresh every N seconds")
    d.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("status", help="print paper account, positions, open orders, market clock")
    s.set_defaults(fn=cmd_status)

    r = sub.add_parser("risk", help="inspect / reset risk state")
    rs = r.add_subparsers(dest="risk_cmd", required=True)
    for name in ("show", "reset"):
        x = rs.add_parser(name)
        x.add_argument("--run-id", default="paper")
    r.set_defaults(fn=cmd_risk)

    dd = sub.add_parser("data", help="manage the local bar cache")
    ds = dd.add_subparsers(dest="data_cmd", required=True)
    f = ds.add_parser("fetch"); f.add_argument("--symbol", required=True, action="append"); f.add_argument("--start", required=True, type=_date); f.add_argument("--end", required=True, type=_date); f.add_argument("--refresh", action="store_true")
    i = ds.add_parser("import"); i.add_argument("--symbol", required=True); i.add_argument("--csv", required=True); i.add_argument("--close-column", default=None, help="for wide close-only files: which column is this symbol")
    ds.add_parser("list")
    dd.set_defaults(fn=cmd_data)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level, settings.secret_values())
    if settings.live_trading and args.cmd != "paper":
        console.print("[yellow]note: LIVE_TRADING=true is set; only the `paper` command with explicit flags can act on it[/yellow]")
    try:
        sys.exit(args.fn(args))
    except KeyboardInterrupt:
        sys.exit(130)
