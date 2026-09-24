"""Execution loop ("reflex" layer) for daily bars. Paper by default; live only through the interlock.

Each cycle:
1. Sync order statuses with the broker; book fills (incl. partial), record closed trades, handle rejections.
2. Pull account equity; feed the risk manager (daily halt, kill switch).
   Kill switch -> liquidate everything, alert, and refuse to trade until a human resets.
3. Find the last *completed* trading session. For every symbol not yet processed for that session: load bars,
   replay the strategy over its warm-up window (restarts need no in-memory state), derive the desired exposure,
   apply the protective stop, build a typed MarketState, and reconcile against the broker's actual position.
4. Every order passes ``RiskManager.check_order`` (deterministic APPROVE/REJECT) and, in live mode, the
   ``LiveInterlock`` gates. At most one market order per symbol per session, with a deterministic
   ``client_order_id`` (``<run_id>-<symbol>-<date>-<entry|exit>``). The state file is written before and after
   every submission; the broker refuses duplicate client ids, so a crash cannot double-order.
5. Every processed (symbol, session) appends a DecisionRecord to ``logs/decisions.jsonl``.

Order timing: whole-share orders use market-on-open (OPG) submitted 19:00-09:28 ET; fractional orders must be
DAY orders at Alpaca, so they are submitted once the market is open. Broker state is authoritative: local state is
reconciled against it, never the other way round.
"""
from __future__ import annotations

import logging
import math
import signal
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from bot.config import Settings, TradingEnv
from bot.data.calendar import NY, is_opg_window, last_completed_session_date, now_ny
from bot.data.loader import BarLoader
from bot.data.store import BarStore
from bot.execution.broker import AssetInfo, Broker, BrokerPosition, OrderInfo, QuoteInfo
from bot.execution.interlock import LiveInterlock
from bot.execution.market_state import MarketState, build_market_state
from bot.execution.state import BotState, StateStore
from bot.monitoring.alerts import Alerter
from bot.monitoring.decisions import DecisionLog, DecisionRecord
from bot.monitoring.logging import log_event
from bot.risk.manager import OrderIntent, RiskDecision, RiskLimits, RiskManager, RiskState, SafeLiveLimits
from bot.strategies.base import Bar, Strategy
from bot.strategies.indicators import atr as atr_series

log = logging.getLogger(__name__)
ATR_PERIOD = 14
PENDING_STATES = ("submitting", "new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding", "held")


class Trader:
    def __init__(self, *, settings: Settings, broker: Broker, loader: BarLoader, bar_store: BarStore,
                 strategy_cls: type[Strategy], params: dict[str, Any], symbols: list[str],
                 state_store: StateStore, alerter: Alerter | None = None, risk_limits: RiskLimits | None = None,
                 run_id: str = "paper", env: TradingEnv = "paper", decision_log: DecisionLog | None = None,
                 interlock: LiveInterlock | None = None, cli_live_flag: bool = False):
        if env not in ("paper", "live"):
            raise ValueError("env must be paper or live")
        if getattr(broker, "env", "paper") != env:
            raise RuntimeError(f"broker env {getattr(broker, 'env', None)!r} does not match requested env {env!r}")
        self.settings = settings
        self.env: TradingEnv = env
        self.broker = broker
        self.loader = loader
        self.bar_store = bar_store
        self.strategy_cls = strategy_cls
        self.params = params
        self.symbols = [s.upper() for s in symbols]
        self.state_store = state_store
        self.alerter = alerter or Alerter()
        self.run_id = run_id
        self.cli_live_flag = cli_live_flag
        self.decision_log = decision_log or DecisionLog(settings.log_dir / f"decisions_{run_id}.jsonl")
        self.state: BotState = state_store.load()
        if not self.state.strategy:
            self.state.run_id, self.state.strategy, self.state.params, self.state.symbols = run_id, strategy_cls.name, dict(params), self.symbols
        elif self.state.strategy != strategy_cls.name or self.state.params != params:
            log.warning("state file was written by %s %s; now running %s %s. Open positions will be managed by the new strategy.",
                        self.state.strategy, self.state.params, strategy_cls.name, params)
            self.state.strategy, self.state.params = strategy_cls.name, dict(params)
        if self.state.env and self.state.env != env:
            raise RuntimeError(f"state file {state_store.path} belongs to env {self.state.env!r}, refusing to use it for {env!r}")
        self.state.env = env
        safe = SafeLiveLimits.from_settings(settings) if (env == "live" and settings.safe_live_test_mode) else None
        self.risk = RiskManager(risk_limits or RiskLimits.from_settings(settings),
                                RiskState.from_dict(self.state.risk) if self.state.risk else None, safe=safe)
        self.interlock = interlock or (LiveInterlock(settings) if env == "live" else None)
        if self.interlock is not None:
            self.interlock.clear_on_startup()   # default startup state: DISARMED
        self._stop = False
        self._asset_cache: dict[str, AssetInfo] = {}
        self._blocked_alerted: set[str] = set()

    # ------------------------------------------------------------------ loop
    def run_forever(self, poll_interval: int | None = None) -> None:
        if self.env == "live" and not self.settings.live_autonomous_trading:
            raise PermissionError("LIVE_AUTONOMOUS_TRADING=false: the live loop is disabled. Use --once cycles while armed, "
                                  "or set LIVE_AUTONOMOUS_TRADING=true deliberately.")
        poll = poll_interval or self.settings.poll_interval_seconds
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        log.info("%s loop started (%s %s on %s, poll %ss, broker=%s)", self.env, self.strategy_cls.name, self.params,
                 self.symbols, poll, self.broker.name)
        self.alerter.send(f"[{self.env.upper()}] bot started", f"{self.strategy_cls.name} {self.params} on {', '.join(self.symbols)}")
        backoff = poll
        try:
            while not self._stop:
                try:
                    self.run_cycle()
                    backoff = poll
                except Exception as e:  # noqa: BLE001 - keep the loop alive, alert, back off
                    log.exception("cycle failed: %s", e)
                    self.alerter.send(f"[{self.env.upper()}] cycle failed", f"{type(e).__name__}: {e}", level="warning")
                    backoff = min(backoff * 2, 3600)
                for _ in range(int(backoff)):
                    if self._stop:
                        break
                    time.sleep(1)
        finally:
            self.alerter.send(f"[{self.env.upper()}] bot stopped", "shutdown")
            log.info("%s loop stopped", self.env)

    def _on_signal(self, *_):
        log.info("shutdown requested; finishing current cycle")
        self._stop = True

    # ----------------------------------------------------------------- cycle
    def run_cycle(self, now: datetime | None = None) -> dict[str, Any]:
        now = (now or now_ny()).astimezone(NY)
        summary: dict[str, Any] = {"ts": now.isoformat(), "env": self.env, "actions": []}
        try:
            self._sync_orders(now)
            acct = self.broker.get_account()
            positions = self.broker.get_positions()
            open_orders = self.broker.get_open_orders()
            self._reconcile_positions(positions)
            events = self.risk.update_equity(pd.Timestamp(now), acct.equity)
            self.state.risk = self.risk.state.to_dict()
            self.bar_store.append_equity(self.run_id, now, acct.equity, acct.cash)
            self.state.equity_log = (self.state.equity_log + [{"ts": now.isoformat(), "equity": acct.equity, "cash": acct.cash}])[-2000:]
            summary.update(equity=acct.equity, cash=acct.cash, positions={s: p.qty for s, p in positions.items()},
                           open_orders=len(open_orders), request_id=self.broker.last_request_id)
            for ev in events:
                if ev.kind == "kill_switch":
                    self._trip_kill_switch(ev.detail)
                    summary["actions"].append("kill_switch")
                elif ev.kind == "daily_halt":
                    log_event(log, f"daily loss limit hit: {ev.detail}", kind="daily_halt", equity=acct.equity)
                    self.alerter.send(f"[{self.env.upper()}] daily loss limit hit", ev.detail, level="warning")
            if self.risk.killed:
                summary["status"] = "killed"
                self._persist(now)
                return summary
            session_date = self._last_completed_session(now)
            summary["session"] = session_date.isoformat()
            for sym in self.symbols:
                if self.state.last_processed.get(sym) == session_date.isoformat():
                    continue
                action, decision = self._process_symbol(sym, session_date, acct, positions, open_orders, now)
                if action:
                    summary["actions"].append(action)
                if decision == "waiting":
                    summary["status"] = "waiting_for_order_window"
            summary.setdefault("status", "ok")
            self.state.last_error = None
        except Exception as e:
            self.state.last_error = f"{now.isoformat()} {type(e).__name__}: {e}"
            self._persist(now)
            raise
        self._persist(now)
        return summary

    # ------------------------------------------------------------ per symbol
    def _process_symbol(self, sym: str, session_date: date, acct, positions: dict[str, BrokerPosition],
                        open_orders: list[OrderInfo], now: datetime) -> tuple[str | None, str]:
        """Returns (action description or None, order_decision of the record)."""
        rec = DecisionRecord(timestamp=now.isoformat(), environment=self.env, strategy=self.strategy_cls.name,
                             symbol=sym, session=session_date.isoformat())
        try:
            return self._decide(sym, session_date, acct, positions, open_orders, now, rec), rec.order_decision
        except Exception as e:  # noqa: BLE001
            rec.exception = f"{type(e).__name__}: {e}"
            rec.broker_request_id = self.broker.last_request_id
            self.decision_log.append(rec)
            raise

    def _decide(self, sym: str, session_date: date, acct, positions, open_orders, now, rec: DecisionRecord) -> str | None:
        strategy = self.strategy_cls(**self.params)
        warmup = strategy.warmup + ATR_PERIOD + 5
        df = self.loader.get_daily(sym, session_date, session_date, warmup=warmup)
        if df.empty or df.index[-1].date() < session_date:
            log.info("%s: bar for %s not available yet (last=%s); will retry", sym, session_date,
                     df.index[-1].date() if not df.empty else None)
            rec.order_decision, rec.notes = "waiting", ["bar for session not available yet"]
            self.decision_log.append(rec)
            return None
        df = df.loc[: pd.Timestamp(session_date, tz=NY) + pd.Timedelta(hours=23)]
        if len(df) < strategy.warmup:
            rec.order_decision, rec.notes = "waiting", [f"insufficient warm-up: {len(df)} bars < {strategy.warmup}"]
            log.warning("%s: insufficient warm-up (%d bars < %d); no signal possible", sym, len(df), strategy.warmup)
            self.decision_log.append(rec)
            self.state.last_processed[sym] = session_date.isoformat()
            return None
        desired, reason = self._replay(strategy, sym, df)
        rec.signal = {"target": desired, "reason": reason}
        last_close = float(df["close"].iloc[-1])
        atr_val = atr_series(df, ATR_PERIOD).iloc[-1]
        atr_val = float(atr_val) if pd.notna(atr_val) else None

        bpos = positions.get(sym)
        cur_qty = bpos.qty if bpos is not None else 0.0
        cur_side = 0 if not cur_qty else (1 if cur_qty > 0 else -1)
        spos = self.state.positions.get(sym)
        rec.current_broker_position = cur_qty

        # protective stop, checked on the completed bar (same rule as the backtester)
        if bpos is not None and spos and spos.get("stop") is not None:
            stop = float(spos["stop"])
            if (cur_side == 1 and last_close <= stop) or (cur_side == -1 and last_close >= stop):
                desired, reason = 0, f"stop {stop:.2f} hit (close {last_close:.2f})"
                self.state.risk_exits.setdefault(sym, []).append(session_date.isoformat())
                rec.notes.append("protective stop breached")

        quote = self._safe_quote(sym)
        clock_open = self._market_open()
        fast, slow = int(self.params.get("fast", 50)), int(self.params.get("slow", 200))
        mstate = build_market_state(symbol=sym, bars=df, now=now, market_open=clock_open, quote=quote, account=acct,
                                    positions=positions, open_orders=open_orders, peak_equity=self.risk.state.peak_equity,
                                    day_start_equity=self.risk.state.day_start_equity, fast=fast, slow=slow)
        rec.market_state = mstate.to_dict()
        if math.isfinite(mstate.stale_data_seconds) and mstate.stale_data_seconds > self.settings.max_stale_data_seconds and clock_open:
            self.alerter.send(f"[{self.env.upper()}] stale market data for {sym}", f"{mstate.stale_data_seconds:.0f}s old", level="warning")

        if self._has_open_order(sym, open_orders):
            log.info("%s: open order pending at broker; not acting on %s", sym, session_date)
            rec.order_decision, rec.notes = "waiting", rec.notes + ["open order pending"]
            self.decision_log.append(rec)
            return None

        action = None
        if desired == cur_side:
            rec.desired_position = cur_qty
            rec.order_decision = "none"
            log.info("%s %s: target %+d matches position; nothing to do (%s)", session_date, sym, desired, reason)
        elif cur_side != 0:
            rec.desired_position = 0.0
            side = "sell" if cur_side == 1 else "buy"
            action = self._submit(sym, abs(cur_qty), side, session_date, "exit", reason, stop=None, acct=acct,
                                  mstate=mstate, positions=positions, open_orders=open_orders, ref_price=last_close, now=now, rec=rec)
        else:
            n_open = len(positions) + sum(1 for o in self.state.orders.values()
                                          if o.get("kind") == "entry" and o.get("status") in PENDING_STATES)
            ok, why = self.risk.can_open(n_open)
            if not ok:
                rec.order_decision, rec.notes = "blocked", rec.notes + [why]
                log_event(log, f"{sym}: entry blocked ({why})", kind="entry_blocked", symbol=sym, why=why)
            else:
                stop_dist = self.risk.stop_distance(atr_val, last_close)
                qty = self.risk.position_qty(acct.equity, last_close, stop_dist, cash_available=acct.cash if desired == 1 else None)
                rec.desired_position = qty * desired
                if qty > 0:
                    stop = last_close - desired * stop_dist
                    action = self._submit(sym, qty, "buy" if desired == 1 else "sell", session_date, "entry", reason, stop=stop,
                                          acct=acct, mstate=mstate, positions=positions, open_orders=open_orders,
                                          ref_price=last_close, now=now, rec=rec)
                else:
                    rec.order_decision, rec.notes = "skip", rec.notes + ["sized to zero"]
                    log.info("%s: sized to 0 shares (equity %.2f, price %.2f); skipping", sym, acct.equity, last_close)
        if rec.order_decision == "waiting":   # tif not available yet -> retry next cycle, do not mark processed
            self.decision_log.append(rec)
            return None
        self.state.last_processed[sym] = session_date.isoformat()
        rec.broker_request_id = rec.broker_request_id or self.broker.last_request_id
        self.decision_log.append(rec)
        self._persist()
        return action

    def _replay(self, strategy: Strategy, sym: str, df: pd.DataFrame) -> tuple[int, str]:
        """Feed history through the strategy; return its current desired exposure."""
        strategy.reset()
        exits = set(self.state.risk_exits.get(sym, []))
        desired, reason = 0, "no signal"
        for ts, row in df.iterrows():
            sig = strategy.on_bar(Bar.from_row(sym, ts, row))
            if sig is not None:
                desired, reason = sig.target, sig.reason
            if ts.date().isoformat() in exits:
                strategy.on_position_closed(sym, "risk exit")
                desired, reason = 0, "flat after risk exit"
        return desired, reason

    # ---------------------------------------------------------------- orders
    def _submit(self, sym: str, qty: float, side: str, session_date: date, kind: str, reason: str, stop: float | None, *,
                acct, mstate: MarketState, positions, open_orders, ref_price: float, now: datetime, rec: DecisionRecord) -> str | None:
        cid = f"{self.run_id}-{sym}-{session_date.isoformat()}-{kind}"
        rec.client_order_id = cid
        if cid in self.state.orders and self.state.orders[cid].get("status") not in ("lost",):
            log.warning("%s: order %s already recorded (status %s); not resubmitting", sym, cid, self.state.orders[cid].get("status"))
            rec.order_decision, rec.notes = "skip", rec.notes + ["order already recorded"]
            return None
        asset = self._asset(sym)
        fractional = abs(qty - round(qty)) > 1e-9
        tif = self._pick_tif(now, fractional=fractional)
        if tif is None:
            rec.order_decision, rec.notes = "waiting", rec.notes + ["waiting for order window (OPG window or market open)"]
            return None
        intent = OrderIntent(sym, side, qty, kind, ref_price, cid, tif, reason)
        env_ok = None
        try:
            env_ok = self.broker.verify_account_env()[0] if hasattr(self.broker, "verify_account_env") else None
        except Exception as e:  # noqa: BLE001
            log.warning("could not verify account env: %s", e)
        bar_current = mstate.last_bar_date == session_date.isoformat()
        decision = self.risk.check_order(
            intent, state=mstate, account=acct, asset=asset, broker_env=getattr(self.broker, "env", "paper"),
            expected_env=self.env, account_is_paper_shaped=(acct.is_paper_account_number if env_ok is None else (self.env == "paper")),
            open_orders=open_orders, known_client_ids=list(self.state.orders), position_qty=mstate.position_qty,
            n_positions=len(positions), tif=tif, now=now, bar_current=bar_current)
        rec.risk_decision = decision.to_dict()
        if not decision.approved:
            rec.order_decision = "blocked"
            log_event(log, f"{sym}: RISK REJECT {decision.code}: {decision.detail}", kind="risk_reject", symbol=sym,
                      code=decision.code, detail=decision.detail, client_order_id=cid)
            key = f"{cid}:{decision.code}"
            if key not in self._blocked_alerted:
                self._blocked_alerted.add(key)
                self.alerter.send(f"[{self.env.upper()}] risk rejected {side.upper()} {qty:g} {sym}", f"{decision.code}: {decision.detail}", level="warning")
            return None
        if self.env == "live":
            gates = self.interlock.check(cli_live_flag=self.cli_live_flag, account=acct,
                                         account_env_ok=(env_ok, "account endpoint") if env_ok is not None else None,
                                         data_fresh=bar_current and decision.approved, risk_manager=self.risk,
                                         risk_last_error=self.state.last_error)
            failed = [g for g in gates if not g.ok]
            if failed:
                rec.order_decision = "blocked"
                rec.notes.append("live interlock: " + "; ".join(f"{g.name} ({g.detail})" for g in failed))
                log_event(log, f"{sym}: LIVE INTERLOCK BLOCKED: {[g.name for g in failed]}", kind="interlock_block", symbol=sym,
                          gates=[g.name for g in failed])
                key = f"{cid}:interlock"
                if key not in self._blocked_alerted:
                    self._blocked_alerted.add(key)
                    self.alerter.send("[LIVE] interlock blocked order", ", ".join(g.name for g in failed), level="warning")
                return None
        reference = mstate.mid if (math.isfinite(mstate.mid) and mstate.mid > 0 and tif == "day") else ref_price
        recd = {"client_order_id": cid, "symbol": sym, "qty": qty, "side": side, "kind": kind, "reason": reason,
                "status": "submitting", "tif": tif, "session": session_date.isoformat(), "submitted_at": now.isoformat(),
                "reference_price": reference, "reference_kind": "quote_mid" if reference is mstate.mid else "last_close", "env": self.env}
        self.state.orders[cid] = recd
        if kind == "entry":
            self.state.positions[sym] = {"side": 1 if side == "buy" else -1, "qty": qty, "stop": stop,
                                         "entry_session": session_date.isoformat(), "entry_price": None,
                                         "reason": reason, "client_order_id": cid}
        self._persist()  # crash after this point -> restart sees "submitting" and checks the broker
        info = self.broker.submit_market_order(sym, qty, side, cid, tif)
        recd.update(status=info.status, broker_id=info.id, request_id=self.broker.last_request_id)
        self._persist()
        rec.order_decision, rec.order_id, rec.broker_request_id = "submit", info.id, self.broker.last_request_id
        log_event(log, f"[{self.env}] submitted {side.upper()} {qty:g} {sym} ({kind}, {tif}): {reason}", kind="order_submitted",
                  symbol=sym, qty=qty, side=side, order_kind=kind, client_order_id=cid, stop=stop, broker_id=info.id,
                  request_id=self.broker.last_request_id, env=self.env)
        self.alerter.send(f"[{self.env.upper()}] order: {side.upper()} {qty:g} {sym}", f"{kind} · {reason}" + (f" · stop {stop:.2f}" if stop else ""))
        if info.status == "rejected":
            self._handle_terminal_unfilled(cid, recd, info)
            rec.fill_result = {"status": "rejected"}
        return f"{side} {qty:g} {sym} ({kind})"

    def _sync_orders(self, now: datetime | None = None) -> None:
        for cid, recd in list(self.state.orders.items()):
            if recd.get("status") in ("filled", "canceled", "expired", "rejected", "lost", "done_for_day"):
                continue
            info = self.broker.get_order_by_client_id(cid)
            if info is None:
                if recd.get("status") == "submitting":
                    log.warning("order %s was never accepted by the broker; marking lost", cid)
                    recd["status"] = "lost"
                    if recd["kind"] == "entry":
                        self.state.positions.pop(recd["symbol"], None)
                    self.state.last_processed.pop(recd["symbol"], None)   # decision can be re-taken
                continue
            if info.status == recd.get("status") and info.filled_qty == recd.get("filled_qty", 0):
                continue
            prev_filled = float(recd.get("filled_qty") or 0)
            recd.update(status=info.status, broker_id=info.id, filled_qty=info.filled_qty, filled_avg_price=info.filled_avg_price)
            sym = recd["symbol"]
            if info.status == "partially_filled" and info.filled_qty > prev_filled:
                log_event(log, f"partial fill {recd['side'].upper()} {info.filled_qty:g}/{info.qty:g} {sym}", kind="partial_fill",
                          symbol=sym, filled=info.filled_qty, qty=info.qty)
                self.alerter.send(f"[{self.env.upper()}] partial fill: {recd['side'].upper()} {info.filled_qty:g}/{info.qty:g} {sym}")
            if info.is_filled or (info.is_terminal and info.filled_qty > 0):
                self._book_fill(cid, recd, info)
            if info.is_terminal and not info.is_filled:
                self._handle_terminal_unfilled(cid, recd, info)
        self._persist()

    def _book_fill(self, cid: str, recd: dict, info: OrderInfo) -> None:
        sym, px = recd["symbol"], info.filled_avg_price or 0.0
        ref = float(recd.get("reference_price") or 0)
        sign = 1 if recd["side"] == "buy" else -1
        slip = sign * (px - ref) / ref * 1e4 if ref > 0 and px > 0 else None
        recd["realized_slippage_bps"] = slip
        log_event(log, f"filled {recd['side'].upper()} {info.filled_qty:g} {sym} @ {px:.4f}", kind="fill", symbol=sym,
                  qty=info.filled_qty, price=px, order_kind=recd["kind"], slippage_bps=slip, reference_kind=recd.get("reference_kind"))
        self.alerter.send(f"[{self.env.upper()}] filled: {recd['side'].upper()} {info.filled_qty:g} {sym} @ {px:.2f}",
                          f"{recd.get('reason', '')}" + (f" · slippage {slip:+.1f}bps vs {recd.get('reference_kind')}" if slip is not None else ""))
        self.decision_log.append(DecisionRecord(timestamp=str(info.filled_at or now_ny().isoformat()), environment=self.env,
                                                strategy=self.state.strategy, symbol=sym, session=recd.get("session", ""),
                                                order_decision="fill", order_id=info.id, client_order_id=cid,
                                                broker_request_id=self.broker.last_request_id,
                                                fill_result={"status": info.status, "filled_qty": info.filled_qty, "price": px},
                                                realized_slippage_bps=slip))
        if recd["kind"] == "entry" and sym in self.state.positions:
            self.state.positions[sym].update(entry_price=px, qty=info.filled_qty, filled_at=str(info.filled_at))
        elif recd["kind"] == "exit":
            pos = self.state.positions.get(sym)
            if pos and pos.get("entry_price"):
                side = pos["side"]
                pnl = side * (px - float(pos["entry_price"])) * info.filled_qty
                self.state.trades.append({"symbol": sym, "side": side, "qty": info.filled_qty, "entry_price": pos["entry_price"],
                                          "exit_price": px, "pnl": pnl, "entry_session": pos.get("entry_session"),
                                          "exit_session": recd.get("session"), "entry_reason": pos.get("reason"),
                                          "exit_reason": recd.get("reason"), "env": self.env})
                remaining = float(pos.get("qty", 0)) - info.filled_qty
                if remaining > 1e-9:
                    pos["qty"] = remaining   # partial exit: keep the remainder, broker reconciliation confirms
                else:
                    self.state.positions.pop(sym, None)

    def _handle_terminal_unfilled(self, cid: str, recd: dict, info: OrderInfo) -> None:
        sym = recd["symbol"]
        log.warning("order %s ended %s (filled %g of %g)", cid, info.status, info.filled_qty, info.qty)
        self.alerter.send(f"[{self.env.upper()}] order {info.status}: {recd['side'].upper()} {recd['qty']:g} {sym}",
                          recd.get("reason", ""), level="warning")
        if recd["kind"] == "entry" and info.filled_qty <= 0:
            self.state.positions.pop(sym, None)

    def _has_open_order(self, sym: str, open_orders: list[OrderInfo] | None = None) -> bool:
        orders = open_orders if open_orders is not None else self.broker.get_open_orders()
        if any(o.symbol == sym and o.is_open for o in orders):
            return True
        return any(r["symbol"] == sym and r.get("status") in PENDING_STATES for r in self.state.orders.values())

    def _reconcile_positions(self, broker_positions: dict[str, BrokerPosition]) -> None:
        for sym, spos in list(self.state.positions.items()):
            if spos.get("entry_price") is None:
                continue  # entry not filled yet
            if sym not in broker_positions:
                log.warning("%s: position in state but not at broker (closed manually?) - dropping local record", sym)
                self.alerter.send(f"[{self.env.upper()}] reconciliation: {sym} missing at broker", "local record dropped", level="warning")
                self.state.positions.pop(sym)
            elif abs(abs(broker_positions[sym].qty) - float(spos.get("qty", 0))) > 1e-6:
                log.warning("%s: broker qty %g != local %s; trusting broker", sym, broker_positions[sym].qty, spos.get("qty"))
                self.alerter.send(f"[{self.env.upper()}] reconciliation: {sym} qty mismatch",
                                  f"broker {broker_positions[sym].qty:g} vs local {spos.get('qty')}; trusting broker", level="warning")
                spos["qty"] = abs(broker_positions[sym].qty)
        for sym in broker_positions:
            if sym not in self.state.positions and sym in self.symbols:
                log.warning("%s: broker holds a position this bot did not open; it will be managed by strategy exits but has no stop", sym)
                bp = broker_positions[sym]
                self.state.positions[sym] = {"side": 1 if bp.qty > 0 else -1, "qty": abs(bp.qty), "stop": None,
                                             "entry_price": bp.avg_entry_price, "entry_session": None, "reason": "adopted"}

    # ------------------------------------------------------------------ misc
    def _trip_kill_switch(self, detail: str) -> None:
        log_event(log, f"KILL SWITCH: {detail} - liquidating all positions", kind="kill_switch", detail=detail, env=self.env)
        self.alerter.send(f"[{self.env.upper()}] KILL SWITCH TRIPPED", f"{detail}\nLiquidating all positions and halting. Run `python -m bot risk reset` to resume.", level="critical")
        try:
            self.broker.cancel_all_orders()
            self.broker.close_all_positions()
        finally:
            self.state.positions.clear()
            self.state.risk = self.risk.state.to_dict()
            self._persist()

    def _last_completed_session(self, now: datetime) -> date:
        try:
            sessions = self.broker.get_sessions(now.date() - timedelta(days=10), now.date())
        except Exception as e:  # noqa: BLE001
            log.warning("calendar unavailable (%s); using weekday fallback", e)
            sessions = None
        return last_completed_session_date(now, sessions)

    def _market_open(self) -> bool:
        try:
            return self.broker.get_clock().is_open
        except Exception as e:  # noqa: BLE001
            log.warning("clock unavailable (%s)", e)
            return False

    def _pick_tif(self, now: datetime, *, fractional: bool) -> str | None:
        """Fractional -> DAY while the market is open. Whole shares -> OPG inside its window, DAY if open, else wait."""
        market_open = self._market_open()
        if fractional:
            return "day" if market_open else None
        if self.settings.order_time_in_force == "day":
            return "day" if market_open else None
        if is_opg_window(now):
            return "opg"
        return "day" if market_open else None

    def _safe_quote(self, sym: str) -> QuoteInfo | None:
        try:
            return self.broker.get_latest_quote(sym)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: quote unavailable (%s)", sym, e)
            return None

    def _asset(self, sym: str) -> AssetInfo | None:
        if sym not in self._asset_cache:
            try:
                self._asset_cache[sym] = self.broker.get_asset(sym)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: asset lookup failed (%s)", sym, e)
                return None
        return self._asset_cache[sym]

    def _persist(self, now: datetime | None = None) -> None:
        if now is not None:
            self.state.last_cycle = now.isoformat()
        self.state.risk = self.risk.state.to_dict()
        self.state_store.save(self.state)


PaperTrader = Trader  # backwards-compatible name
