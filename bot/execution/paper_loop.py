"""Paper-trading loop (daily bars).

Each cycle:
1. Sync order statuses with the broker; book fills / record closed trades.
2. Pull account equity; feed the risk manager (daily halt, kill switch).
   Kill switch -> liquidate everything, alert, and refuse to trade until a human resets.
3. Find the last *completed* trading session. For every symbol not yet processed for
   that session: load bars (cache + provider), replay the strategy over its warm-up
   window (so restarts need no in-memory state), derive the desired exposure, apply
   the protective stop, and reconcile against the broker's actual position.
4. Submit at most one market order per symbol per session with a deterministic
   client_order_id (``<run_id>-<symbol>-<date>-<entry|exit>``). The state file is
   written before and after the submission; Alpaca rejects duplicate client ids, so
   a crash at any point cannot produce a second order for the same decision.

Orders default to market-on-open (OPG) which matches the backtester's "fill at
next open" assumption; outside the OPG window while the market is open, a plain
day market order is used instead (logged).
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from bot.config import Settings
from bot.data.calendar import NY, is_opg_window, last_completed_session_date, now_ny
from bot.data.loader import BarLoader
from bot.data.store import BarStore
from bot.execution.broker import Broker, BrokerPosition
from bot.execution.state import BotState, StateStore
from bot.monitoring.alerts import Alerter
from bot.monitoring.logging import log_event
from bot.risk.manager import RiskLimits, RiskManager, RiskState
from bot.strategies.base import Bar, Strategy
from bot.strategies.indicators import atr as atr_series

log = logging.getLogger(__name__)
ATR_PERIOD = 14


class PaperTrader:
    def __init__(self, *, settings: Settings, broker: Broker, loader: BarLoader, bar_store: BarStore,
                 strategy_cls: type[Strategy], params: dict[str, Any], symbols: list[str],
                 state_store: StateStore, alerter: Alerter | None = None, risk_limits: RiskLimits | None = None,
                 run_id: str = "paper"):
        self.settings = settings
        self.broker = broker
        self.loader = loader
        self.bar_store = bar_store
        self.strategy_cls = strategy_cls
        self.params = params
        self.symbols = [s.upper() for s in symbols]
        self.state_store = state_store
        self.alerter = alerter or Alerter()
        self.run_id = run_id
        self.state: BotState = state_store.load()
        if not self.state.strategy:
            self.state.run_id, self.state.strategy, self.state.params, self.state.symbols = run_id, strategy_cls.name, dict(params), self.symbols
        elif self.state.strategy != strategy_cls.name or self.state.params != params:
            log.warning("state file was written by %s %s; now running %s %s. Open positions will be managed by the new strategy.",
                        self.state.strategy, self.state.params, strategy_cls.name, params)
            self.state.strategy, self.state.params = strategy_cls.name, dict(params)
        self.risk = RiskManager(risk_limits or RiskLimits.from_settings(settings),
                                RiskState.from_dict(self.state.risk) if self.state.risk else None)
        self._stop = False

    # ------------------------------------------------------------------ loop
    def run_forever(self, poll_interval: int | None = None) -> None:
        poll = poll_interval or self.settings.poll_interval_seconds
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        log.info("paper loop started (%s %s on %s, poll %ss, broker=%s paper=%s)", self.strategy_cls.name, self.params,
                 self.symbols, poll, self.broker.name, self.broker.is_paper)
        backoff = poll
        while not self._stop:
            try:
                self.run_cycle()
                backoff = poll
            except Exception as e:  # noqa: BLE001 - keep the loop alive, alert, back off
                log.exception("cycle failed: %s", e)
                self.alerter.send("Cycle failed", f"{type(e).__name__}: {e}", level="warning")
                backoff = min(backoff * 2, 3600)
            for _ in range(int(backoff)):
                if self._stop:
                    break
                time.sleep(1)
        log.info("paper loop stopped")

    def _on_signal(self, *_):
        log.info("shutdown requested; finishing current cycle")
        self._stop = True

    # ----------------------------------------------------------------- cycle
    def run_cycle(self, now: datetime | None = None) -> dict[str, Any]:
        now = (now or now_ny()).astimezone(NY)
        summary: dict[str, Any] = {"ts": now.isoformat(), "actions": []}
        try:
            self._sync_orders()
            acct = self.broker.get_account()
            positions = self.broker.get_positions()
            self._reconcile_positions(positions)
            events = self.risk.update_equity(pd.Timestamp(now), acct.equity)
            self.state.risk = self.risk.state.to_dict()
            self.bar_store.append_equity(self.run_id, now, acct.equity, acct.cash)
            self.state.equity_log = (self.state.equity_log + [{"ts": now.isoformat(), "equity": acct.equity, "cash": acct.cash}])[-2000:]
            summary.update(equity=acct.equity, cash=acct.cash, positions={s: p.qty for s, p in positions.items()})
            for ev in events:
                if ev.kind == "kill_switch":
                    self._trip_kill_switch(ev.detail)
                    summary["actions"].append("kill_switch")
                elif ev.kind == "daily_halt":
                    log_event(log, f"daily loss limit hit: {ev.detail}", kind="daily_halt", equity=acct.equity)
                    self.alerter.send("Daily loss limit hit", ev.detail, level="warning")
            if self.risk.killed:
                summary["status"] = "killed"
                self._persist(now)
                return summary
            session_date = self._last_completed_session(now)
            summary["session"] = session_date.isoformat()
            tif = self._pick_tif(now)
            for sym in self.symbols:
                if self.state.last_processed.get(sym) == session_date.isoformat():
                    continue
                if tif is None:
                    summary["status"] = "waiting_for_opg_window"
                    break
                action = self._process_symbol(sym, session_date, acct.equity, acct.cash, positions, tif)
                if action:
                    summary["actions"].append(action)
            summary.setdefault("status", "ok")
            self.state.last_error = None
        except Exception as e:
            self.state.last_error = f"{now.isoformat()} {type(e).__name__}: {e}"
            self._persist(now)
            raise
        self._persist(now)
        return summary

    # ------------------------------------------------------------ per symbol
    def _process_symbol(self, sym: str, session_date: date, equity: float, cash: float,
                        positions: dict[str, BrokerPosition], tif: str) -> str | None:
        strategy = self.strategy_cls(**self.params)
        warmup = strategy.warmup + ATR_PERIOD + 5
        df = self.loader.get_daily(sym, session_date, session_date, warmup=warmup)
        if df.empty or df.index[-1].date() < session_date:
            log.info("%s: bar for %s not available yet (last=%s); will retry", sym, session_date,
                     df.index[-1].date() if not df.empty else None)
            return None
        df = df.loc[: pd.Timestamp(session_date, tz=NY) + pd.Timedelta(hours=23)]
        desired, reason = self._replay(strategy, sym, df)
        last_close = float(df["close"].iloc[-1])
        atr_val = atr_series(df, ATR_PERIOD).iloc[-1]
        atr_val = float(atr_val) if pd.notna(atr_val) else None

        bpos = positions.get(sym)
        cur_side = 0 if bpos is None else (1 if bpos.qty > 0 else -1)
        spos = self.state.positions.get(sym)

        # protective stop, checked on the completed bar (same rule as the backtester)
        if bpos is not None and spos and spos.get("stop") is not None:
            stop = float(spos["stop"])
            if (cur_side == 1 and last_close <= stop) or (cur_side == -1 and last_close >= stop):
                desired, reason = 0, f"stop {stop:.2f} hit (close {last_close:.2f})"
                self.state.risk_exits.setdefault(sym, []).append(session_date.isoformat())

        if self._has_open_order(sym):
            log.info("%s: open order pending at broker; not acting on %s", sym, session_date)
            return None

        action = None
        if desired == cur_side:
            log.info("%s %s: target %+d matches position; nothing to do (%s)", session_date, sym, desired, reason)
        elif cur_side != 0:
            side = "sell" if cur_side == 1 else "buy"
            action = self._submit(sym, abs(bpos.qty), side, session_date, "exit", reason, stop=None)
        else:
            n_open = len(positions) + sum(1 for o in self.state.orders.values()
                                          if o.get("kind") == "entry" and o.get("status") in ("submitting", "new", "accepted", "pending_new"))
            ok, why = self.risk.can_open(n_open)
            if not ok:
                log_event(log, f"{sym}: entry blocked ({why})", kind="entry_blocked", symbol=sym, why=why)
            else:
                stop_dist = self.risk.stop_distance(atr_val, last_close)
                qty = self.risk.position_qty(equity, last_close, stop_dist, cash_available=cash if desired == 1 else None)
                if qty > 0:
                    stop = last_close - desired * stop_dist
                    action = self._submit(sym, qty, "buy" if desired == 1 else "sell", session_date, "entry", reason, stop=stop)
                else:
                    log.info("%s: sized to 0 shares (equity %.2f, price %.2f); skipping", sym, equity, last_close)
        self.state.last_processed[sym] = session_date.isoformat()
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
    def _submit(self, sym: str, qty: int, side: str, session_date: date, kind: str, reason: str, stop: float | None) -> str:
        cid = f"{self.run_id}-{sym}-{session_date.isoformat()}-{kind}"
        if cid in self.state.orders and self.state.orders[cid].get("status") not in ("lost",):
            log.warning("%s: order %s already recorded (status %s); not resubmitting", sym, cid, self.state.orders[cid].get("status"))
            return None
        tif = self._pick_tif(now_ny()) or self.settings.order_time_in_force
        rec = {"client_order_id": cid, "symbol": sym, "qty": qty, "side": side, "kind": kind, "reason": reason,
               "status": "submitting", "tif": tif, "session": session_date.isoformat(), "submitted_at": now_ny().isoformat()}
        self.state.orders[cid] = rec
        if kind == "entry":
            self.state.positions[sym] = {"side": 1 if side == "buy" else -1, "qty": qty, "stop": stop,
                                         "entry_session": session_date.isoformat(), "entry_price": None,
                                         "reason": reason, "client_order_id": cid}
        self._persist()  # crash after this point -> restart sees "submitting" and checks the broker
        info = self.broker.submit_market_order(sym, qty, side, cid, tif)
        rec.update(status=info.status, broker_id=info.id)
        self._persist()
        log_event(log, f"submitted {side.upper()} {qty} {sym} ({kind}, {tif}): {reason}", kind="order_submitted",
                  symbol=sym, qty=qty, side=side, order_kind=kind, client_order_id=cid, stop=stop)
        self.alerter.send(f"Order: {side.upper()} {qty} {sym}", f"{kind} · {reason}" + (f" · stop {stop:.2f}" if stop else ""))
        return f"{side} {qty} {sym} ({kind})"

    def _sync_orders(self) -> None:
        for cid, rec in list(self.state.orders.items()):
            if rec.get("status") in ("filled", "canceled", "expired", "rejected", "lost", "done_for_day"):
                continue
            info = self.broker.get_order_by_client_id(cid)
            if info is None:
                if rec.get("status") == "submitting":
                    log.warning("order %s was never accepted by the broker; marking lost", cid)
                    rec["status"] = "lost"
                    if rec["kind"] == "entry":
                        self.state.positions.pop(rec["symbol"], None)
                    # allow the decision to be re-taken on the next cycle
                    self.state.last_processed.pop(rec["symbol"], None)
                continue
            if info.status == rec.get("status"):
                continue
            rec.update(status=info.status, broker_id=info.id, filled_qty=info.filled_qty, filled_avg_price=info.filled_avg_price)
            sym = rec["symbol"]
            if info.is_filled:
                px = info.filled_avg_price or 0.0
                log_event(log, f"filled {rec['side'].upper()} {info.filled_qty} {sym} @ {px:.2f}", kind="fill",
                          symbol=sym, qty=info.filled_qty, price=px, order_kind=rec["kind"])
                self.alerter.send(f"Filled: {rec['side'].upper()} {info.filled_qty} {sym} @ {px:.2f}", rec.get("reason", ""))
                if rec["kind"] == "entry" and sym in self.state.positions:
                    self.state.positions[sym].update(entry_price=px, qty=info.filled_qty, filled_at=str(info.filled_at))
                elif rec["kind"] == "exit":
                    pos = self.state.positions.pop(sym, None)
                    if pos and pos.get("entry_price"):
                        side = pos["side"]
                        pnl = side * (px - float(pos["entry_price"])) * info.filled_qty
                        self.state.trades.append({"symbol": sym, "side": side, "qty": info.filled_qty,
                                                  "entry_price": pos["entry_price"], "exit_price": px, "pnl": pnl,
                                                  "entry_session": pos.get("entry_session"), "exit_session": rec.get("session"),
                                                  "entry_reason": pos.get("reason"), "exit_reason": rec.get("reason")})
            elif info.is_terminal:  # canceled / expired / rejected
                log.warning("order %s ended %s without filling", cid, info.status)
                self.alerter.send(f"Order {info.status}: {rec['side'].upper()} {rec['qty']} {sym}", rec.get("reason", ""), level="warning")
                if rec["kind"] == "entry":
                    self.state.positions.pop(sym, None)
        self._persist()

    def _has_open_order(self, sym: str) -> bool:
        if any(o.symbol == sym for o in self.broker.get_open_orders()):
            return True
        return any(r["symbol"] == sym and r.get("status") in ("submitting", "new", "accepted", "pending_new", "partially_filled")
                   for r in self.state.orders.values())

    def _reconcile_positions(self, broker_positions: dict[str, BrokerPosition]) -> None:
        for sym, spos in list(self.state.positions.items()):
            if spos.get("entry_price") is None:
                continue  # entry not filled yet
            if sym not in broker_positions:
                log.warning("%s: position in state but not at broker (closed manually?) - dropping local record", sym)
                self.state.positions.pop(sym)
            elif abs(broker_positions[sym].qty) != int(spos.get("qty", 0)):
                log.warning("%s: broker qty %d != local %s; trusting broker", sym, broker_positions[sym].qty, spos.get("qty"))
                spos["qty"] = abs(broker_positions[sym].qty)
        for sym in broker_positions:
            if sym not in self.state.positions and sym in self.symbols:
                log.warning("%s: broker holds a position this bot did not open; it will be managed by strategy exits but has no stop", sym)
                bp = broker_positions[sym]
                self.state.positions[sym] = {"side": 1 if bp.qty > 0 else -1, "qty": abs(bp.qty), "stop": None,
                                             "entry_price": bp.avg_entry_price, "entry_session": None, "reason": "adopted"}

    # ------------------------------------------------------------------ misc
    def _trip_kill_switch(self, detail: str) -> None:
        log_event(log, f"KILL SWITCH: {detail} - liquidating all positions", kind="kill_switch", detail=detail)
        self.alerter.send("KILL SWITCH TRIPPED", f"{detail}\nLiquidating all positions and halting. Run `python -m bot risk reset` to resume.", level="critical")
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

    def _pick_tif(self, now: datetime) -> str | None:
        """OPG inside its window; otherwise a DAY order if the market is open; otherwise wait."""
        if self.settings.order_time_in_force == "day":
            return "day"
        if is_opg_window(now):
            return "opg"
        try:
            if self.broker.get_clock().is_open:
                return "day"
        except Exception as e:  # noqa: BLE001
            log.warning("clock unavailable (%s)", e)
        return None

    def _persist(self, now: datetime | None = None) -> None:
        if now is not None:
            self.state.last_cycle = now.isoformat()
        self.state.risk = self.risk.state.to_dict()
        self.state_store.save(self.state)
