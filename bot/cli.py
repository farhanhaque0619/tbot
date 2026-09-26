"""Command line interface: python -m bot <command> ...

Environment selection: every broker-facing command takes --paper (default) or --live. A live command
additionally needs TRADING_ENV=live, live credentials, and - for anything that can submit an order - an armed
interlock (see `live arm`). `status`, `doctor`, `live check` are read-only and never submit orders.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.table import Table

from bot import __version__
from bot.config import LIVE_CONFIRMATION_PHRASE, Settings, TradingEnv, banner, get_settings
from bot.monitoring.health import HealthReport
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


def _env_from_args(args, settings: Settings) -> TradingEnv:
    """--live wins only if TRADING_ENV=live too; anything else is paper."""
    want_live = bool(getattr(args, "live", False))
    if want_live and settings.trading_env != "live":
        raise SystemExit("--live given but TRADING_ENV is not 'live'. Refusing (set TRADING_ENV=live deliberately).")
    if not want_live and settings.trading_env == "live" and getattr(args, "cmd", "") in ("trade", "paper"):
        console.print("[yellow]TRADING_ENV=live is set but --live was not passed: running PAPER.[/yellow]")
    return "live" if want_live else "paper"


def _print_banner(env: TradingEnv) -> None:
    console.print(f"[bold red]{banner(env)}[/bold red]" if env == "live" else f"[bold green]{banner(env)}[/bold green]")


def _make_loader(settings: Settings, env: TradingEnv | None = None, *, need_provider: bool = False):
    from bot.data.loader import BarLoader
    from bot.data.store import BarStore

    store = BarStore(settings.data_db_path)
    provider = None
    data_env: TradingEnv | None = env if env and settings.has_credentials(env) else ("paper" if settings.has_credentials("paper") else None)
    if data_env is not None:
        from bot.data.providers import AlpacaBarProvider
        provider = AlpacaBarProvider(settings, env=data_env)
    elif need_provider:
        raise SystemExit("No Alpaca credentials configured (copy .env.example to .env and fill ALPACA_PAPER_*). "
                         "Offline: import CSV bars with `python -m bot data import`.")
    return store, BarLoader(store, provider, adjustment=settings.data_adjustment)


def _broker(settings: Settings, env: TradingEnv):
    from bot.execution.broker import AlpacaBroker

    if not settings.has_credentials(env):
        cs = settings.credential_status(env)
        raise SystemExit(f"{env} credentials missing: key_present={cs['key_present']} secret_present={cs['secret_present']} "
                         f"(set ALPACA_{env.upper()}_API_KEY / ALPACA_{env.upper()}_SECRET_KEY in .env)")
    return AlpacaBroker.for_env(settings, env)


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
    if args.cash < 1000 and not risk.allow_fractional:
        console.print("[yellow]note: whole-share sizing with a small account will rarely produce a trade; set ALLOW_FRACTIONAL=true[/yellow]")
    console.print(f"[dim]costs: slippage {costs.slippage_bps}bps + half-spread {costs.spread_bps / 2}bps per side "
                  f"(round trip {costs.round_trip_bps():.1f}bps); risk/trade {risk.risk_per_trade_pct:.1%}, "
                  f"max position {risk.max_position_pct:.0%}, kill switch {risk.max_drawdown_pct:.0%} DD, fractional={risk.allow_fractional}[/dim]")
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
                              initial_cash=args.cash, costs=costs, risk=risk, start=ts_start, end=ts_end, min_trades=args.min_trades)
        except ValueError as e:
            console.print(f"[yellow]walk-forward skipped: {e}[/yellow]")
            return 0
        print_walkforward(wf, console)
        if not args.no_save:
            for p in save_walkforward(wf, out_dir, tag):
                console.print(f"[dim]wrote {p}[/dim]")
    return 0


def cmd_trade(args) -> int:
    """Run the execution loop. Paper unless --live (and then only through the interlock)."""
    from bot.execution.interlock import LiveInterlock
    from bot.execution.paper_loop import Trader
    from bot.execution.state import StateStore
    from bot.monitoring.alerts import Alerter
    from bot.risk import RiskLimits, SafeLiveLimits
    from bot.strategies import get_strategy_class

    settings = get_settings()
    env = _env_from_args(args, settings)
    _print_banner(env)
    strategy_cls = get_strategy_class(args.strategy)
    params = _parse_params(args.param)
    strategy_cls(**params)  # validate params early
    store, loader = _make_loader(settings, env, need_provider=True)
    broker = _broker(settings, env)
    acct = broker.get_account()
    ok, why = broker.verify_account_env()
    if not ok:
        raise SystemExit(f"REFUSING: {why}")
    console.print(f"[bold]{env.upper()}[/bold] · {strategy_cls.name} {params} · {args.symbol} · account …{acct.account_number[-4:]} "
                  f"status={acct.status} equity {acct.equity:,.2f} cash {acct.cash:,.2f} · {why}")
    run_id = args.run_id or env
    if env == "live":
        safe = SafeLiveLimits.from_settings(settings) if settings.safe_live_test_mode else None
        if safe is None:
            console.print("[bold red]SAFE_LIVE_TEST_MODE is OFF. This is not recommended for a small account.[/bold red]")
        else:
            _print_safe_summary(safe)
        il = LiveInterlock(settings)
        armed, detail = il.is_armed()
        console.print(f"interlock: {'ARMED' if armed else 'DISARMED'} ({detail}); LIVE_AUTONOMOUS_TRADING={settings.live_autonomous_trading}")
        console.print("[yellow]note: starting the process clears any previous arm; run `python -m bot live arm` in another terminal, "
                      "then a cycle can submit.[/yellow]")
    trader = Trader(settings=settings, broker=broker, loader=loader, bar_store=store, strategy_cls=strategy_cls,
                    params=params, symbols=args.symbol, state_store=StateStore(settings.state_dir / f"{run_id}.json"),
                    alerter=Alerter(settings.discord_webhook_url.get_secret_value()),
                    risk_limits=RiskLimits.from_settings(settings), run_id=run_id, env=env, cli_live_flag=bool(getattr(args, "live", False)))
    if args.once:
        if env == "live" and getattr(args, "arm_now", False):
            _arm_interactive(settings)
        summary = trader.run_cycle()
        console.print_json(json.dumps(summary, default=str))
        return 0
    trader.run_forever(args.poll)
    return 0


def _print_safe_summary(safe) -> None:
    t = Table(title="SAFE_LIVE_TEST_MODE limits (operator-owned; the bot cannot change these)", header_style="bold")
    t.add_column("Limit"); t.add_column("Value")
    for k, v in safe.summary():
        t.add_row(k, v)
    console.print(t)


def _account_table(env: TradingEnv, settings: Settings, broker, probe_symbol: str) -> tuple[Table, dict[str, Any], HealthReport]:
    """Read-only account / connectivity probe. Returns (rich table, info dict, HealthReport). Never orders."""
    from bot.data.calendar import last_completed_session_date, now_ny
    from bot.monitoring.health import DEGRADED, FAILED, OK, HealthReport, classify_bar_currency, classify_quote_age

    info: dict[str, Any] = {"env": env}
    rep = HealthReport()
    t = Table(title=f"{env.upper()} account / connectivity", header_style="bold", show_lines=False)
    t.add_column("Item"); t.add_column("Value")
    cs = settings.credential_status(env)
    t.add_row("credentials", f"key_present={cs['key_present']} secret_present={cs['secret_present']} prefix={cs['key_prefix']}* prefix_ok={cs['key_prefix_ok']}")
    rep.add("credentials", OK if cs["key_present"] and cs["secret_present"] and cs["key_prefix_ok"] else FAILED, "presence/prefix")
    a = broker.get_account()
    ok, why = broker.verify_account_env()
    info.update(account_ok=ok, healthy=a.healthy)
    t.add_row("account", f"…{a.account_number[-4:]}  status={a.status}  env-check: {'OK' if ok else 'MISMATCH'} ({why})")
    rep.add("account_env_matches", OK if ok else FAILED, why)
    rep.add("account_healthy", OK if a.healthy else FAILED,
            f"status={a.status} trading_blocked={a.trading_blocked} account_blocked={a.account_blocked} suspended={a.trade_suspended_by_user}")
    t.add_row("equity / cash / buying power", f"{a.equity:,.2f} / {a.cash:,.2f} / {a.buying_power:,.2f} {a.currency}")
    t.add_row("blocked?", f"trading_blocked={a.trading_blocked} account_blocked={a.account_blocked} transfers_blocked={a.transfers_blocked} suspended_by_user={a.trade_suspended_by_user}")
    t.add_row("margin / shorting / PDT", f"multiplier={a.multiplier:g} shorting_enabled={a.shorting_enabled} pattern_day_trader={a.pattern_day_trader} daytrade_count={a.daytrade_count}")
    pos = broker.get_positions()
    oo = broker.get_open_orders()
    t.add_row("positions / open orders", f"{len(pos)} / {len(oo)}")
    for sym, pp in pos.items():
        t.add_row(f"  position {sym}", f"{pp.qty:+g} @ {pp.avg_entry_price:.2f} (now {pp.current_price:.2f}, value {pp.market_value:,.2f})")
    for o in oo:
        t.add_row(f"  open order {o.client_order_id}", f"{o.side} {o.qty:g} {o.symbol} [{o.status}] tif={o.time_in_force}")
    rep.add("positions_and_orders_readable", OK, f"{len(pos)} positions, {len(oo)} open orders")
    c = broker.get_clock()
    market_open = bool(c.is_open)
    t.add_row("market clock", f"{'OPEN' if market_open else 'closed'} · now {c.timestamp:%Y-%m-%d %H:%M %Z} · next open {c.next_open:%m-%d %H:%M} · next close {c.next_close:%m-%d %H:%M}")
    info["market_open"] = market_open
    rep.add("market_clock", OK, "open" if market_open else "closed")
    # --- quote (freshness only matters while the market is open)
    quote_age = None
    try:
        q = broker.get_latest_quote(probe_symbol)
        if q is not None:
            quote_age = (now_ny() - q.timestamp).total_seconds()
            t.add_row(f"latest quote {probe_symbol}", f"bid {q.bid:.2f} ask {q.ask:.2f} spread {q.spread_bps:.1f}bps · {quote_age:.0f}s old · feed={settings.data_feed}")
        else:
            t.add_row(f"latest quote {probe_symbol}", "unavailable")
        lvl, detail = classify_quote_age(quote_age, market_open=market_open, max_stale_seconds=settings.max_stale_data_seconds)
        rep.add("quote_freshness", lvl, detail, required=False)
    except Exception as e:  # noqa: BLE001
        t.add_row(f"latest quote {probe_symbol}", f"error: {type(e).__name__}: {e}")
        rep.add("quote_freshness", FAILED, f"{type(e).__name__}: {e}", required=False)
    info["quote_age_s"] = quote_age
    # --- asset metadata (required: fractionable/tradable gates depend on it)
    try:
        asset = broker.get_asset(probe_symbol)
        t.add_row(f"asset {probe_symbol}", f"tradable={asset.tradable} fractionable={asset.fractionable} shortable={asset.shortable} marginable={asset.marginable} class={asset.asset_class}")
        rep.add("asset_metadata", OK if asset.tradable else DEGRADED, f"tradable={asset.tradable} fractionable={asset.fractionable}")
    except Exception as e:  # noqa: BLE001
        t.add_row(f"asset {probe_symbol}", f"error: {type(e).__name__}: {e}")
        rep.add("asset_metadata", FAILED, f"{type(e).__name__}: {e}")
    # --- calendar (required) and daily bars (required source; currency of the last bar is DEGRADED-only)
    sess = None
    try:
        sessions = broker.get_sessions(date.today() - timedelta(days=10), date.today())
        sess = last_completed_session_date(now_ny(), sessions)
        t.add_row("calendar", f"{len(sessions)} sessions in last 10 days · last completed session {sess} · source={sessions[-1].source if sessions else '-'}")
        rep.add("calendar", OK if sessions else FAILED, f"{len(sessions)} sessions")
    except Exception as e:  # noqa: BLE001
        t.add_row("calendar", f"error: {type(e).__name__}: {e}")
        rep.add("calendar", FAILED, f"{type(e).__name__}: {e}")
    try:
        store, loader = _make_loader(settings, env)
        ref = sess or date.today()
        bars = loader.get_daily(probe_symbol, ref - timedelta(days=10), ref)
        if bars.empty:  # nothing in the window: distinguish "stale cache" (DEGRADED) from "no data at all" (FAILED)
            bars = store.get_bars(probe_symbol, adjustment=settings.data_adjustment)
        last_bar = bars.index[-1].date() if len(bars) else None
        if sess is not None:
            lvl, detail = classify_bar_currency(last_bar, sess)
        else:
            lvl, detail = (DEGRADED, f"last bar {last_bar}; session unknown (calendar failed)") if last_bar else (FAILED, "no bars")
        t.add_row(f"daily bars {probe_symbol}", f"last bar {last_bar} · {detail} · adjustment={settings.data_adjustment}")
        rep.add("daily_bars", lvl, detail)
        info["bar_current"] = lvl == OK
    except Exception as e:  # noqa: BLE001
        t.add_row(f"daily bars {probe_symbol}", f"error: {type(e).__name__}: {e}")
        rep.add("daily_bars", FAILED, f"{type(e).__name__}: {e}")
        info["bar_current"] = False
    t.add_row("last request id", str(broker.last_request_id))
    t.add_row("health", rep.level + ("" if rep.level == "OK" else ": " + "; ".join(f"{c.name}={c.level}" for c in rep.problems())))
    info["health"] = rep
    return t, info, rep


def cmd_status(args) -> int:
    settings = get_settings()
    env: TradingEnv = "live" if args.live else "paper"
    _print_banner(env)
    broker = _broker(settings, env)
    t, _, rep = _account_table(env, settings, broker, args.symbol)
    console.print(t)
    if env == "live":
        from bot.execution.interlock import LiveInterlock
        armed, detail = LiveInterlock(settings).is_armed()
        console.print(f"interlock: {'ARMED' if armed else 'DISARMED'} ({detail}) · LIVE_AUTONOMOUS_TRADING={settings.live_autonomous_trading} · SAFE_LIVE_TEST_MODE={settings.safe_live_test_mode}")
    console.print(f"[dim]read-only: no order was submitted · health: {rep.level}[/dim]")
    return 0


def cmd_doctor(args) -> int:
    """Configuration + connectivity health check. Never submits an order.

    Exit codes: 0 OK · 2 DEGRADED (works, but something non-required or transient is off) · 1 FAILED."""
    from bot.monitoring.health import DEGRADED, FAILED, OK, HealthReport

    settings = get_settings()
    env: TradingEnv = "live" if args.live else "paper"
    _print_banner(env)
    rep = HealthReport()
    for e in ("paper", "live"):
        cs = settings.credential_status(e)
        console.print(f"credentials[{e}]: key_present={cs['key_present']} secret_present={cs['secret_present']} prefix_ok={cs['key_prefix_ok']}")
        if e == env and not (cs["key_present"] and cs["secret_present"]):
            rep.add(f"credentials_{e}", FAILED, f"{e} credentials missing")
        if cs["key_present"] and not cs["key_prefix_ok"]:
            rep.add(f"credentials_{e}_prefix", FAILED, f"{e} key has the wrong prefix for its slot (paper=PK…, live=AK…)")
    if Path(".env").exists():
        import subprocess
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], capture_output=True, text=True).returncode == 0
        console.print(f".env present, git-tracked={tracked}")
        if tracked:
            rep.add("env_file_not_tracked", FAILED, ".env is TRACKED by git - remove it from the index and rotate every key in it")
    if rep.level == FAILED:
        _print_health(rep)
        return rep.exit_code
    try:
        broker = _broker(settings, env)
        t, info, acct_rep = _account_table(env, settings, broker, args.symbol)
        console.print(t)
        rep.checks.extend(acct_rep.checks)
    except SystemExit as e:
        rep.add("broker_connectivity", FAILED, str(e))
    except Exception as e:  # noqa: BLE001
        rep.add("broker_connectivity", FAILED, f"{type(e).__name__}: {e}")
    if env == "live":
        from bot.execution.interlock import LiveInterlock
        from bot.risk import SafeLiveLimits
        il = LiveInterlock(settings)
        armed, detail = il.is_armed()
        console.print(f"interlock: {'ARMED' if armed else 'DISARMED'} ({detail}); LIVE_AUTONOMOUS_TRADING={settings.live_autonomous_trading}")
        if settings.safe_live_test_mode:
            _print_safe_summary(SafeLiveLimits.from_settings(settings))
            rep.add("safe_live_test_mode", OK, "ON")
        else:
            rep.add("safe_live_test_mode", DEGRADED, "SAFE_LIVE_TEST_MODE=false (not recommended for a small account)")
    _print_health(rep)
    return rep.exit_code


def _print_health(rep) -> None:
    for c in rep.problems():
        colour = "red" if c.level == "FAILED" else "yellow"
        console.print(f"[{colour}]{'✗' if c.level == 'FAILED' else '!'} {c.name}: {c.level} — {c.detail}[/{colour}]")
    colour = {"OK": "green", "DEGRADED": "yellow", "FAILED": "red"}[rep.level]
    console.print(f"[{colour}]doctor: {rep.level}[/{colour}] (read-only, no orders submitted; exit code {rep.exit_code})")


def _arm_interactive(settings: Settings) -> None:
    from bot.execution.interlock import LiveInterlock
    from bot.risk import SafeLiveLimits

    if not sys.stdin.isatty():
        raise SystemExit("arming needs an interactive terminal")
    il = LiveInterlock(settings)
    if settings.safe_live_test_mode:
        _print_safe_summary(SafeLiveLimits.from_settings(settings))
        ack = input("Type ACK to acknowledge these limits: ").strip() == "ACK"
    else:
        console.print("[bold red]SAFE_LIVE_TEST_MODE is OFF.[/bold red]")
        ack = input("Type ACK-UNSAFE to acknowledge running without safe-mode caps: ").strip() == "ACK-UNSAFE"
    typed = input(f"Type exactly: {LIVE_CONFIRMATION_PHRASE}\n> ")
    st = il.arm(typed_phrase=typed, acknowledged=ack)
    console.print(f"[bold red]ARMED[/bold red] until {st.expires_at} (UTC). Disarm with `python -m bot live disarm`. Any restart disarms.")


def cmd_live(args) -> int:
    from bot.execution.interlock import LiveInterlock

    settings = get_settings()
    il = LiveInterlock(settings)
    if args.live_cmd == "check":
        _print_banner("live")
        gates_ok = True
        account = None
        env_ok = None
        data_fresh = None
        try:
            broker = _broker(settings, "live")
            t, info, _rep = _account_table("live", settings, broker, args.symbol)
            console.print(t)
            account = broker.get_account()
            env_ok = broker.verify_account_env()
            data_fresh = bool(info.get("bar_current")) and (info.get("quote_age_s") is None or info["quote_age_s"] <= settings.max_stale_data_seconds or not info.get("market_open"))
        except SystemExit as e:
            console.print(f"[red]{e}[/red]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]live connectivity failed: {type(e).__name__}: {e}[/red]")
        from bot.execution.state import StateStore
        from bot.risk import RiskLimits, RiskManager, RiskState, SafeLiveLimits
        st = StateStore(settings.state_dir / "live.json").load()
        rm = RiskManager(RiskLimits.from_settings(settings), RiskState.from_dict(st.risk) if st.risk else None,
                         safe=SafeLiveLimits.from_settings(settings) if settings.safe_live_test_mode else None)
        gates = il.check(cli_live_flag=True, account=account, account_env_ok=env_ok, data_fresh=data_fresh, risk_manager=rm,
                         risk_last_error=st.last_error)
        gt = Table(title="Live interlock gates (all must pass before any live order)", header_style="bold")
        gt.add_column("Gate"); gt.add_column("OK"); gt.add_column("Detail")
        for g in gates:
            gates_ok &= g.ok
            gt.add_row(g.name, "[green]PASS[/green]" if g.ok else "[red]FAIL[/red]", g.detail)
        console.print(gt)
        console.print(f"LIVE_AUTONOMOUS_TRADING={settings.live_autonomous_trading} (multi-cycle loop {'enabled' if settings.live_autonomous_trading else 'disabled'})")
        console.print("[dim]read-only: no order was submitted[/dim]")
        return 0 if gates_ok else 1
    if args.live_cmd == "arm":
        _print_banner("live")
        _arm_interactive(settings)
        return 0
    if args.live_cmd == "disarm":
        console.print("disarmed" if il.disarm() else "was not armed")
        return 0
    return 1


def cmd_smoke(args) -> int:
    """PAPER-ONLY execution smoke test. Submits one tiny fractional order and closes it (or ack+cancel when closed)."""
    from bot.execution.smoke import PaperSmokeTest, SmokeRefusal

    settings = get_settings()
    if settings.trading_env != "paper":
        raise SystemExit(f"smoke test is paper-only; TRADING_ENV={settings.trading_env!r}. Refusing.")
    _print_banner("paper")
    broker = _broker(settings, "paper")
    console.print(f"smoke test: {args.symbol} ≈ ${args.notional:.2f} fractional DAY order on PAPER account, then close it. "
                  f"Market closed -> ack/duplicate/cancel only. Run id: {args.run_id or '(timestamp)'}")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("non-interactive: pass --yes to confirm a paper order")
        if input("Type PAPER to submit a paper order: ").strip() != "PAPER":
            raise SystemExit("aborted")
    try:
        t = PaperSmokeTest(settings=settings, broker=broker, symbol=args.symbol, notional=args.notional,
                           wait_seconds=args.wait, run_id=args.run_id)
    except SmokeRefusal as e:
        console.print(f"[red]REFUSED: {e}[/red]")
        return 1
    rep = t.run()
    tbl = Table(title=f"paper smoke test · {rep.run_id}", header_style="bold")
    for c in ("Step", "Status", "Detail", "Order id", "Request id"):
        tbl.add_column(c)
    for st in rep.steps:
        colour = {"PASS": "green", "FAIL": "red", "SKIP": "yellow"}[st.status]
        tbl.add_row(st.name, f"[{colour}]{st.status}[/{colour}]", st.detail[:110], st.order_id or "", st.request_id or "")
    console.print(tbl)
    console.print(f"[{'green' if rep.ok else 'red'}]smoke: {'PASS' if rep.ok else 'FAIL'}[/] · market_open={rep.market_open} · "
                  f"report reports/smoke_paper_{rep.run_id}.json · state {settings.state_dir}/{rep.run_id}.json")
    return 0 if rep.ok else 1


def cmd_dashboard(args) -> int:
    from bot.monitoring.dashboard import show

    settings = get_settings()
    show(settings.state_dir / f"{args.run_id}.json", watch=args.watch)
    return 0


def cmd_risk(args) -> int:
    from bot.execution.state import StateStore

    settings = get_settings()
    store = StateStore(settings.state_dir / f"{args.run_id}.json")
    state = store.load()
    if args.risk_cmd == "show":
        console.print_json(json.dumps({"run_id": args.run_id, "env": state.env, "risk": state.risk or {}, "last_error": state.last_error,
                                       "positions": state.positions, "pending_orders": [c for c, o in state.orders.items() if o.get("status") in ("submitting", "new", "accepted", "partially_filled")]}, default=str))
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


def _calendar(settings: Settings, store, broker=None):
    from bot.data.sessions import SessionCalendar
    cal = SessionCalendar.from_store(store)
    if broker is not None:
        try:
            cal.sync_from_broker(broker, store, date.today() - timedelta(days=400), date.today() + timedelta(days=60))
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]calendar sync failed ({type(e).__name__}); using cached/rule calendar[/yellow]")
    return cal


def cmd_data(args) -> int:
    settings = get_settings()
    if args.data_cmd == "fetch" and getattr(args, "timeframe", "1d") == "1m":
        from bot.data.minute import fetch_minute_history
        from bot.data.providers import AlpacaBarProvider, DataPlanError

        env: TradingEnv = "paper" if settings.has_credentials("paper") else "live"
        if not settings.has_credentials(env):
            raise SystemExit("No Alpaca credentials configured.")
        store, _ = _make_loader(settings)
        provider = AlpacaBarProvider(settings, env=env)
        feed = args.feed or settings.data_feed
        cal = _calendar(settings, store, _broker(settings, env) if settings.has_credentials(env) else None)
        for sym in args.symbol:
            try:
                summ = fetch_minute_history(store, provider, sym, args.start, args.end, feed=feed, calendar=cal, force=args.refresh)
            except DataPlanError as e:
                raise SystemExit(f"DATA PLAN: {e}") from e
            console.print(f"{sym.upper()} 1m/{feed}: {summ.sessions_fetched}/{summ.sessions_requested} sessions fetched, {summ.bars_written} bars written, "
                          f"incomplete sessions: {len(summ.incomplete)} {[str(d) for d in summ.incomplete[:5]]}")
        return 0
    if args.data_cmd == "check" and getattr(args, "timeframe", "1d") == "1m":
        from bot.data.quality import check_minute_frame
        from bot.data.store import BarStore

        store = BarStore(settings.data_db_path)
        cal = _calendar(settings, store)
        for sym in args.symbol:
            df = store.get_minute_bars(sym, feed=args.feed)
            console.print(check_minute_frame(sym, df, cal, feed=args.feed or "any").render())
        return 0
    if args.data_cmd == "fetch":
        _, loader = _make_loader(settings, need_provider=True)
        for sym in args.symbol:
            df = loader.get_daily(sym, args.start, args.end, refresh=args.refresh)
            console.print(f"{sym.upper()}: {len(df)} bars cached ({df.index[0].date() if len(df) else '-'} → {df.index[-1].date() if len(df) else '-'})")
    elif args.data_cmd == "import":
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
    elif args.data_cmd == "check":
        from bot.data.quality import check_symbol
        from bot.data.store import BarStore

        store = BarStore(settings.data_db_path)
        for sym in args.symbol:
            rep = check_symbol(store, sym, adjustment=settings.data_adjustment)
            console.print(rep.render())
    return 0


def cmd_universe(args) -> int:
    from bot.data.universe import Universe, build_tier3, read_candidates

    settings = get_settings()
    u = Universe.load(settings.universe_path)
    if args.universe_cmd == "show":
        console.print(f"tier1 {u.tier1}\ntier2 {u.tier2}\ntier3 {u.tier3} (built_on={u.tier3_built_on})\n"
                      f"total {len(u.symbols)} / cap {u.plan_cap} (DATA_PLAN={settings.data_plan})")
        try:
            u.check_cap(settings.data_plan)
            console.print("[green]within the streaming cap[/green]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        return 0
    if args.universe_cmd == "build":
        env: TradingEnv = "paper"
        broker = _broker(settings, env)
        store, loader = _make_loader(settings, env, need_provider=True)
        asof = args.asof or date.today()

        def spread_samples(sym: str) -> list[float]:
            # Sampled quoted spread: one latest quote now (Basic plan has no cheap 60-day quote history). Documented in D4.
            q = broker.get_latest_quote(sym)
            return [q.spread_bps] if q else []
        selected, evaluated = build_tier3(read_candidates(args.candidates), daily_bars=lambda s, a, b: loader.get_daily(s, a, b),
                                          asset_info=broker.get_asset, spread_samples=spread_samples, asof=asof, n=args.n)
        t = Table(title=f"tier 3 candidates as of {asof}", header_style="bold")
        for c in ("Symbol", "Price", "Median $vol (M)", "Spread bps", "Eligible", "Reasons"):
            t.add_column(c)
        for c in sorted(evaluated, key=lambda c: -c.median_dollar_volume):
            t.add_row(c.symbol, f"{c.price:.2f}", f"{c.median_dollar_volume / 1e6:,.0f}", f"{c.median_spread_bps:.1f}" if c.median_spread_bps != float("inf") else "-",
                      "yes" if c.eligible else "no", "; ".join(c.reasons))
        console.print(t)
        u.tier3 = [c.symbol for c in selected]
        u.tier3_built_on = asof.isoformat()
        for c in selected:
            u.sectors[c.symbol] = c.sector
        u.check_cap(settings.data_plan)
        p = u.save()
        console.print(f"wrote {p} with {len(u.tier3)} tier-3 symbols; review and commit it")
        return 0
    return 1


def cmd_review(args) -> int:
    from bot.research.review import write_review

    settings = get_settings()
    out = write_review(settings, run_id=args.run_id, out_dir=Path(args.out))
    console.print(f"wrote {out}")
    console.print(Path(out).read_text())
    return 0


def cmd_research(args) -> int:
    from bot.research.cli import run_research

    return run_research(args, console)


# ------------------------------------------------------------------------------ parser
def _add_env_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true", help="paper environment (default)")
    g.add_argument("--live", action="store_true", help="LIVE environment (real money; read-only commands still never order)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bot", description=f"tbot {__version__}: backtest + paper/live-trade strategies on Alpaca")
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

    for name, help_ in (("trade", "run the execution loop (paper by default; --live needs the interlock)"),
                        ("paper", "alias for `trade` in the paper environment")):
        t = sub.add_parser(name, help=help_)
        t.add_argument("--strategy", required=True)
        t.add_argument("--symbol", required=True, action="append")
        t.add_argument("--param", action="append")
        t.add_argument("--once", action="store_true", help="run one cycle and exit (cron-friendly)")
        t.add_argument("--poll", type=int, default=None, help="seconds between cycles (default POLL_INTERVAL_SECONDS)")
        t.add_argument("--run-id", default=None, help="state/log namespace (default: paper|live)")
        if name == "trade":
            _add_env_flags(t)
            t.add_argument("--arm-now", action="store_true", help="with --live --once: arm interactively right before the cycle")
        t.set_defaults(fn=cmd_trade)

    s = sub.add_parser("status", help="account, positions, open orders, clock, data freshness (read-only)")
    _add_env_flags(s)
    s.add_argument("--symbol", default="SPY", help="probe symbol for quote/bar freshness")
    s.set_defaults(fn=cmd_status)

    d = sub.add_parser("doctor", help="configuration + connectivity health check (read-only)")
    _add_env_flags(d)
    d.add_argument("--symbol", default="SPY")
    d.set_defaults(fn=cmd_doctor)

    lv = sub.add_parser("live", help="live interlock: check (read-only) / arm / disarm")
    ls = lv.add_subparsers(dest="live_cmd", required=True)
    lc = ls.add_parser("check"); lc.add_argument("--symbol", default="SPY")
    ls.add_parser("arm"); ls.add_parser("disarm")
    lv.set_defaults(fn=cmd_live)

    sm = sub.add_parser("smoke", help="PAPER-ONLY execution smoke test: tiny fractional order, fill, reconcile, close (no --live exists)")
    sm.add_argument("--paper", action="store_true", help="accepted for symmetry; the smoke test is always paper")
    sm.add_argument("--symbol", default="SPY")
    sm.add_argument("--notional", type=float, default=2.0, help="target dollars for the entry (max 25)")
    sm.add_argument("--wait", type=int, default=180, help="seconds to wait for each fill")
    sm.add_argument("--run-id", default=None, help="must start with 'smoke-'; default is a timestamp")
    sm.add_argument("--yes", action="store_true", help="skip the interactive PAPER confirmation")
    sm.set_defaults(fn=cmd_smoke)

    db = sub.add_parser("dashboard", help="show equity, positions, trades from the state file")
    db.add_argument("--run-id", default="paper")
    db.add_argument("--watch", type=int, default=None, help="refresh every N seconds")
    db.set_defaults(fn=cmd_dashboard)

    r = sub.add_parser("risk", help="inspect / reset risk state")
    rs = r.add_subparsers(dest="risk_cmd", required=True)
    for name in ("show", "reset"):
        x = rs.add_parser(name)
        x.add_argument("--run-id", default="paper")
    r.set_defaults(fn=cmd_risk)

    dd = sub.add_parser("data", help="manage the local bar cache")
    ds = dd.add_subparsers(dest="data_cmd", required=True)
    f = ds.add_parser("fetch"); f.add_argument("--symbol", required=True, action="append"); f.add_argument("--start", required=True, type=_date); f.add_argument("--end", required=True, type=_date); f.add_argument("--refresh", action="store_true")
    f.add_argument("--timeframe", choices=["1d", "1m"], default="1d"); f.add_argument("--feed", choices=["sip", "iex"], default=None)
    i = ds.add_parser("import"); i.add_argument("--symbol", required=True); i.add_argument("--csv", required=True); i.add_argument("--close-column", default=None, help="for wide close-only files: which column is this symbol")
    ds.add_parser("list")
    c = ds.add_parser("check", help="data-quality report: gaps, holidays, splits, duplicates"); c.add_argument("--symbol", required=True, action="append")
    c.add_argument("--timeframe", choices=["1d", "1m"], default="1d"); c.add_argument("--feed", choices=["sip", "iex"], default=None)
    dd.set_defaults(fn=cmd_data)

    un = sub.add_parser("universe", help="show or build the trading universe (config/universe.yaml)")
    un.add_argument("universe_cmd", choices=["show", "build"])
    un.add_argument("--candidates", default="config/tier3_candidates.txt")
    un.add_argument("--n", type=int, default=15)
    un.add_argument("--asof", type=_date, default=None)
    un.set_defaults(fn=cmd_universe)

    rv = sub.add_parser("review", help="post-session review report (read-only; proposes, never deploys)")
    rv.add_argument("--run-id", default="paper")
    rv.add_argument("--out", default="reports")
    rv.set_defaults(fn=cmd_review)

    rr = sub.add_parser("research", help="research harness: compare, surface, regimes (brain layer; never orders)")
    rr.add_argument("research_cmd", choices=["compare", "surface", "regimes", "hypotheses"])
    rr.add_argument("--symbol", action="append", default=None)
    rr.add_argument("--start", type=_date, default=None)
    rr.add_argument("--end", type=_date, default=None)
    rr.add_argument("--strategy", default=None)
    rr.add_argument("--cash", type=float, default=100_000.0)
    rr.add_argument("--out", default="reports")
    rr.set_defaults(fn=cmd_research)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level, settings.secret_values())
    try:
        sys.exit(args.fn(args))
    except KeyboardInterrupt:
        sys.exit(130)
