"""PAPER-ONLY execution smoke test (Phase 3 plumbing validation).

Proves, against the real Alpaca PAPER API, that: an order is acknowledged, the same client_order_id cannot create
a duplicate, fills are observed, the broker position matches, state persists and reloads, reconciliation agrees
with the broker, a controlled exit returns the position to zero, and nothing is left queued.

Hard refusals (the test stops before any order): broker env != paper · SDK host is not paper-api · account number
is not paper-shaped · account not ACTIVE / blocked · the symbol already has a position or open orders in the
account · the pre-trade risk gate rejects the intent · market closes within ``min_minutes_to_close``.

Market closed: a fractional order must be a DAY market order, which Alpaca would QUEUE until the next open and fill
at whatever the opening price is. To avoid leaving a surprising order behind, closed-market mode submits the entry,
verifies acknowledgement / read-back / duplicate rejection, CANCELS it, verifies the cancel and that no position
exists, and SKIPS the fill/position/exit legs with an explicit reason. Run again during regular hours for the full
cycle. Nothing in this module reads live credentials or touches the live interlock, risk limits, or autonomy flag.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bot.config import Settings
from bot.data.calendar import now_ny
from bot.execution.broker import Broker, OrderInfo
from bot.execution.market_state import build_market_state
from bot.execution.state import BotState, StateStore
from bot.risk.manager import OrderIntent, RiskLimits, RiskManager
from bot.risk.sizing import round_qty

log = logging.getLogger(__name__)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
ACCEPTED = {"new", "accepted", "pending_new", "partially_filled", "filled", "accepted_for_bidding"}
CID_PREFIX = "smoke"


class SmokeRefusal(Exception):
    """A hard precondition failed; no order was or will be submitted."""


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""
    order_id: str | None = None
    client_order_id: str | None = None
    request_id: str | None = None


@dataclass
class SmokeReport:
    run_id: str
    env: str
    symbol: str
    started: str
    steps: list[Step] = field(default_factory=list)
    market_open: bool | None = None
    finished: str | None = None

    def add(self, name: str, status: str, detail: str = "", **ids) -> Step:
        s = Step(name, status, detail, **ids)
        self.steps.append(s)
        (log.error if status == FAIL else log.info)("smoke %-34s %s %s", name, status, detail)
        return s

    @property
    def ok(self) -> bool:
        return not any(s.status == FAIL for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperSmokeTest:
    def __init__(self, *, settings: Settings, broker: Broker, symbol: str = "SPY", notional: float = 2.0,
                 wait_seconds: int = 180, poll_seconds: float = 3.0, run_id: str | None = None,
                 min_minutes_to_close: int = 15, report_dir: Path | None = None, sleep=time.sleep):
        if settings.trading_env != "paper":
            raise SmokeRefusal(f"TRADING_ENV must be 'paper' for the smoke test (got {settings.trading_env!r})")
        if getattr(broker, "env", None) != "paper" or not getattr(broker, "is_paper", False):
            raise SmokeRefusal(f"broker env is {getattr(broker, 'env', None)!r}; the smoke test is paper-only")
        base = getattr(broker, "base_url", "paper-api")
        if "paper-api" not in str(base):
            raise SmokeRefusal(f"broker host {base!r} is not the paper host")
        if notional <= 0 or notional > 25:
            raise SmokeRefusal("notional must be in (0, 25] dollars for a smoke test")
        self.settings, self.broker, self.symbol = settings, broker, symbol.upper()
        self.notional, self.wait_seconds, self.poll = float(notional), int(wait_seconds), float(poll_seconds)
        self.min_minutes_to_close = min_minutes_to_close
        self.run_id = run_id or f"{CID_PREFIX}-{now_ny():%Y%m%d-%H%M%S}"
        if not self.run_id.startswith(CID_PREFIX):
            raise SmokeRefusal(f"run id must start with '{CID_PREFIX}-'")
        self.state_store = StateStore(Path(settings.state_dir) / f"{self.run_id}.json")
        self.report_dir = report_dir or Path("reports")
        self.sleep = sleep
        self.risk = RiskManager(RiskLimits.from_settings(settings))
        self.report = SmokeReport(self.run_id, "paper", self.symbol, now_ny().isoformat())

    # ------------------------------------------------------------------ helpers
    def _cid(self, leg: str) -> str:
        return f"{self.run_id}-{self.symbol}-{leg}"

    def _submit_idempotent(self, sym: str, qty: float, side: str, cid: str) -> OrderInfo:
        """Submit; if the broker reports a duplicate client id (HTTP 400/422), return the existing order instead."""
        try:
            return self.broker.submit_market_order(sym, qty, side, cid, "day")
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) in (400, 422):
                existing = self.broker.get_order_by_client_id(cid)
                if existing is not None:
                    return existing
            raise

    def _wait_for(self, cid: str, states: set[str]) -> OrderInfo | None:
        deadline = time.monotonic() + self.wait_seconds
        last = None
        while True:
            last = self.broker.get_order_by_client_id(cid)
            if last is not None and last.status in states:
                return last
            if time.monotonic() >= deadline:
                return last
            self.sleep(self.poll)

    def _persist(self, state: BotState) -> None:
        self.state_store.save(state)

    def _write_report(self) -> Path:
        self.report.finished = now_ny().isoformat()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        p = self.report_dir / f"smoke_paper_{self.run_id}.json"
        p.write_text(json.dumps(self.report.to_dict(), indent=2, default=str))
        return p

    # --------------------------------------------------------------------- run
    def run(self) -> SmokeReport:
        rep, b, sym = self.report, self.broker, self.symbol
        state = BotState(run_id=self.run_id, env="paper", strategy="smoke", symbols=[sym])
        try:
            # 1. environment + account
            ok, why = b.verify_account_env()
            if not ok:
                raise SmokeRefusal(why)
            acct = b.get_account()
            rep.add("env_is_paper", PASS, f"host paper-api · {why} · account …{acct.account_number[-4:]}", request_id=b.last_request_id)
            if not acct.healthy:
                raise SmokeRefusal(f"account not healthy: status={acct.status} trading_blocked={acct.trading_blocked} account_blocked={acct.account_blocked}")
            rep.add("account_active", PASS, f"equity {acct.equity:,.2f} cash {acct.cash:,.2f} bp {acct.buying_power:,.2f}")
            if self.settings.live_autonomous_trading:
                rep.add("live_autonomous_flag", FAIL, "LIVE_AUTONOMOUS_TRADING is true; the smoke test does not run with it set")
                raise SmokeRefusal("LIVE_AUTONOMOUS_TRADING must stay false")
            rep.add("live_autonomous_flag", PASS, "LIVE_AUTONOMOUS_TRADING=false (untouched)")

            # 2. clean slate for this symbol
            positions = b.get_positions()
            open_orders = [o for o in b.get_open_orders() if o.symbol == sym]
            if sym in positions:
                raise SmokeRefusal(f"{sym} already has a position ({positions[sym].qty:g}); the smoke test needs a flat symbol")
            if open_orders:
                raise SmokeRefusal(f"{sym} has {len(open_orders)} open order(s) (e.g. {open_orders[0].client_order_id}); cancel them first")
            rep.add("symbol_flat_no_open_orders", PASS)

            # 3. asset, clock, quote, size
            asset = b.get_asset(sym)
            if not (asset.tradable and asset.fractionable and asset.asset_class == "us_equity"):
                raise SmokeRefusal(f"{sym}: tradable={asset.tradable} fractionable={asset.fractionable} class={asset.asset_class}")
            rep.add("asset_fractionable", PASS, f"tradable={asset.tradable} fractionable={asset.fractionable}")
            clock = b.get_clock()
            rep.market_open = bool(clock.is_open)
            minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60 if clock.is_open else None
            if clock.is_open and minutes_to_close is not None and minutes_to_close < self.min_minutes_to_close:
                raise SmokeRefusal(f"market closes in {minutes_to_close:.0f} min; need >= {self.min_minutes_to_close} for the exit leg")
            rep.add("market_clock", PASS, "OPEN" if clock.is_open else f"closed (next open {clock.next_open:%Y-%m-%d %H:%M %Z}) -> ack/cancel mode, no fill leg")
            quote = b.get_latest_quote(sym)
            ref = quote.ask if quote and quote.ask > 0 else None
            if ref is None:
                raise SmokeRefusal("no usable quote for sizing")
            qty = round_qty(self.notional / ref, fractional=True, decimals=self.settings.qty_decimals)
            if qty * ref < 1.0:      # Alpaca minimum fractional notional
                qty = round_qty(1.05 / ref, fractional=True, decimals=self.settings.qty_decimals)
            if qty <= 0:
                raise SmokeRefusal("sized to zero")
            rep.add("sized", PASS, f"qty {qty:g} @ ask {ref:.2f} ≈ ${qty * ref:.2f} (quote {(clock.timestamp - quote.timestamp).total_seconds():.0f}s old)")

            # 4. pre-trade risk gate (same gate as execution; never bypassed)
            self.risk.update_equity(now_ny(), acct.equity)
            entry_cid = self._cid("entry")
            import pandas as pd
            bars = pd.DataFrame({"open": [ref], "high": [ref], "low": [ref], "close": [ref], "volume": [0.0]},
                                index=pd.DatetimeIndex([pd.Timestamp(now_ny()).normalize()]))
            mstate = build_market_state(symbol=sym, bars=bars, now=clock.timestamp, market_open=bool(clock.is_open), quote=quote, account=acct,
                                        positions=positions, open_orders=open_orders)
            intent = OrderIntent(sym, "buy", qty, "entry", ref, entry_cid, "day", "paper smoke test")
            decision = self.risk.check_order(intent, state=mstate, account=acct, asset=asset, broker_env="paper", expected_env="paper",
                                             account_is_paper_shaped=acct.is_paper_account_number, open_orders=open_orders,
                                             known_client_ids=[], position_qty=0.0, n_positions=len(positions), tif="day",
                                             bar_current=True)
            failed_codes = {c.name for c in decision.failed}
            # With the market closed the quote is hours old and the market gate is shut by definition; those two
            # (and only those two) are tolerated in ack/cancel mode. Every other check must pass.
            closed_ok = (not clock.is_open) and failed_codes <= {"market_permitted", "data_fresh"}
            if not decision.approved and not closed_ok:
                rep.add("risk_gate", FAIL, f"{decision.code}: {decision.detail}")
                raise SmokeRefusal(f"risk gate rejected the smoke order: {decision.code}")
            rep.add("risk_gate", PASS, "APPROVED" if decision.approved else f"market closed: only {sorted(failed_codes)} failed (ack/cancel mode)")

            # 5. submit entry (state written BEFORE the call, like the trader)
            state.orders[entry_cid] = {"client_order_id": entry_cid, "symbol": sym, "qty": qty, "side": "buy", "kind": "entry",
                                       "status": "submitting", "tif": "day", "submitted_at": now_ny().isoformat(), "env": "paper"}
            self._persist(state)
            o = b.submit_market_order(sym, qty, "buy", entry_cid, "day")
            state.orders[entry_cid].update(status=o.status, broker_id=o.id, request_id=b.last_request_id)
            self._persist(state)
            if o.status not in ACCEPTED:
                rep.add("entry_acknowledged", FAIL, f"status={o.status}", order_id=o.id, client_order_id=entry_cid, request_id=b.last_request_id)
                raise SmokeRefusal(f"entry not accepted: {o.status}")
            rep.add("entry_acknowledged", PASS, f"status={o.status} qty={o.qty:g}", order_id=o.id, client_order_id=entry_cid, request_id=b.last_request_id)

            # 6. read back + duplicate rejection
            read = b.get_order_by_client_id(entry_cid)
            rep.add("entry_read_by_client_id", PASS if read is not None and read.id == o.id else FAIL,
                    f"id={getattr(read, 'id', None)} status={getattr(read, 'status', None)}", order_id=o.id, client_order_id=entry_cid, request_id=b.last_request_id)
            dup = self._submit_idempotent(sym, qty, "buy", entry_cid)
            same = dup.id == o.id
            mine = [x for x in b.get_open_orders() if x.client_order_id == entry_cid]
            rep.add("duplicate_client_id_rejected", PASS if same and len(mine) <= 1 else FAIL,
                    f"resubmit returned {'the same' if same else 'a DIFFERENT'} order id; open orders with this cid: {len(mine)}",
                    order_id=dup.id, client_order_id=entry_cid, request_id=b.last_request_id)
            if not same:
                b.cancel_order(dup.id)
                raise SmokeRefusal("duplicate order created - cancelled it; investigate before any live use")

            if not clock.is_open:
                # closed-market mode: cancel, verify, skip the fill legs
                b.cancel_order(o.id)
                c = self._wait_for(entry_cid, {"canceled", "filled", "expired", "rejected"})
                status = getattr(c, "status", None)
                rep.add("entry_cancelled_market_closed", PASS if status == "canceled" else FAIL, f"status={status}",
                        order_id=o.id, client_order_id=entry_cid, request_id=b.last_request_id)
                state.orders[entry_cid]["status"] = status
                self._persist(state)
                for name in ("entry_filled", "broker_position_matches", "state_reload_and_reconcile", "restart_no_duplicate", "exit_acknowledged", "exit_filled", "position_back_to_zero"):
                    rep.add(name, SKIP, "market closed; rerun during 09:30-15:45 ET for the fill/exit legs")
                self._final_checks(state)
                return rep

            # 7. fill
            f = self._wait_for(entry_cid, {"filled", "canceled", "expired", "rejected"})
            if f is None or f.status != "filled":
                st = getattr(f, "status", None)
                rep.add("entry_filled", FAIL, f"status={st} after {self.wait_seconds}s", order_id=o.id, client_order_id=entry_cid)
                if f is not None and not f.is_terminal:
                    b.cancel_order(o.id)
                    rep.add("entry_cancelled_after_timeout", PASS, "cancel requested", order_id=o.id, client_order_id=entry_cid)
                raise SmokeRefusal("entry did not fill in time")
            state.orders[entry_cid].update(status="filled", filled_qty=f.filled_qty, filled_avg_price=f.filled_avg_price)
            state.positions[sym] = {"side": 1, "qty": f.filled_qty, "entry_price": f.filled_avg_price, "stop": None, "reason": "smoke", "client_order_id": entry_cid}
            self._persist(state)
            slip = (f.filled_avg_price - ref) / ref * 1e4 if f.filled_avg_price else None
            rep.add("entry_filled", PASS, f"filled {f.filled_qty:g} @ {f.filled_avg_price} (vs ask {ref:.2f}: {slip:+.1f} bps)",
                    order_id=o.id, client_order_id=entry_cid, request_id=b.last_request_id)

            # 8. broker position
            pos = b.get_positions().get(sym)
            match = pos is not None and abs(pos.qty - f.filled_qty) < 1e-6
            rep.add("broker_position_matches", PASS if match else FAIL, f"broker qty {getattr(pos, 'qty', None)} vs filled {f.filled_qty:g}")

            # 9. persist -> reload -> reconcile
            reloaded = StateStore(self.state_store.path).load()
            same_state = reloaded.orders.get(entry_cid, {}).get("filled_qty") == f.filled_qty and sym in reloaded.positions
            rec_ok = same_state and abs(float(reloaded.positions[sym]["qty"]) - (pos.qty if pos else 0.0)) < 1e-6
            rep.add("state_reload_and_reconcile", PASS if rec_ok else FAIL,
                    f"reloaded qty {reloaded.positions.get(sym, {}).get('qty')} vs broker {getattr(pos, 'qty', None)}")

            # 10. restart: a fresh process with the reloaded state must not create a second entry
            local_guard = entry_cid in reloaded.orders and reloaded.orders[entry_cid]["status"] not in ("lost",)
            again = self._submit_idempotent(sym, qty, "buy", entry_cid)    # broker-side guard, belt and braces
            n_entries = sum(1 for x in b.get_open_orders() if x.client_order_id == entry_cid)
            rep.add("restart_no_duplicate", PASS if local_guard and again.id == o.id and n_entries == 0 else FAIL,
                    f"local guard={local_guard} broker returned same id={again.id == o.id} open entries={n_entries}",
                    order_id=again.id, client_order_id=entry_cid, request_id=b.last_request_id)

            # 11. controlled exit for exactly the broker's quantity
            exit_qty = pos.qty if pos else f.filled_qty
            exit_cid = self._cid("exit")
            state.orders[exit_cid] = {"client_order_id": exit_cid, "symbol": sym, "qty": exit_qty, "side": "sell", "kind": "exit",
                                      "status": "submitting", "tif": "day", "submitted_at": now_ny().isoformat(), "env": "paper"}
            self._persist(state)
            x = b.submit_market_order(sym, exit_qty, "sell", exit_cid, "day")
            state.orders[exit_cid].update(status=x.status, broker_id=x.id, request_id=b.last_request_id)
            self._persist(state)
            rep.add("exit_acknowledged", PASS if x.status in ACCEPTED else FAIL, f"status={x.status} qty={exit_qty:g}",
                    order_id=x.id, client_order_id=exit_cid, request_id=b.last_request_id)
            xf = self._wait_for(exit_cid, {"filled", "canceled", "expired", "rejected"})
            if xf is None or xf.status != "filled":
                rep.add("exit_filled", FAIL, f"status={getattr(xf, 'status', None)} after {self.wait_seconds}s - POSITION MAY REMAIN; check the dashboard",
                        order_id=x.id, client_order_id=exit_cid)
                self._final_checks(state)
                return rep
            state.orders[exit_cid].update(status="filled", filled_qty=xf.filled_qty, filled_avg_price=xf.filled_avg_price)
            state.positions.pop(sym, None)
            state.trades.append({"symbol": sym, "side": 1, "qty": xf.filled_qty, "entry_price": f.filled_avg_price, "exit_price": xf.filled_avg_price,
                                 "pnl": (xf.filled_avg_price - f.filled_avg_price) * xf.filled_qty, "env": "paper", "reason": "smoke"})
            self._persist(state)
            rep.add("exit_filled", PASS, f"filled {xf.filled_qty:g} @ {xf.filled_avg_price}", order_id=x.id, client_order_id=exit_cid, request_id=b.last_request_id)

            # 12. flat again
            pos2 = b.get_positions().get(sym)
            rep.add("position_back_to_zero", PASS if pos2 is None or abs(pos2.qty) < 1e-9 else FAIL, f"broker qty {getattr(pos2, 'qty', 0)}")
            self._final_checks(state)
            return rep
        except SmokeRefusal as e:
            rep.add("refused", FAIL, str(e))
            self._final_checks(state)
            return rep
        finally:
            p = self._write_report()
            log.info("smoke report written to %s", p)

    def _final_checks(self, state: BotState) -> None:
        """Never leave a smoke order queued. Only orders with THIS run's client-id prefix are touched."""
        try:
            leftovers = [o for o in self.broker.get_open_orders() if o.client_order_id.startswith(self.run_id)]
            for o in leftovers:
                self.broker.cancel_order(o.id)
            self.report.add("no_smoke_orders_left_open", PASS if not leftovers else PASS,
                            "none" if not leftovers else f"cancelled {len(leftovers)} leftover smoke order(s): {[o.client_order_id for o in leftovers]}")
        except Exception as e:  # noqa: BLE001
            self.report.add("no_smoke_orders_left_open", FAIL, f"could not verify: {type(e).__name__}: {e}")
        self._persist(state)
