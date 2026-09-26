"""RiskEngine (Phase 2/4, spec §7): wraps the existing RiskManager (extends, never forks), adds an ExposureLedger,
PDT accounting, intent admission and the throttle. `check_order` on the RiskManager remains the final gate for every
OrderIntent including protective legs. Nothing here can raise a limit; the throttle only tightens."""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from bot.core.intents import TradeIntent
from bot.core.policy import RiskPolicy
from bot.risk.manager import OrderIntent, RiskDecision, RiskLimits, RiskManager, RiskState, SafeLiveLimits

log = logging.getLogger(__name__)
INTRADAY_MODULES = {"M2"}


@dataclass
class Slice:
    module_id: str
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    opened_session: date | None = None
    protective_order_id: str | None = None
    unprotected: bool = False
    unprotected_overnight: bool = False


@dataclass
class ExposureLedger:
    """Positions per (module, symbol) slice plus in-flight orders. Prices come from the caller (last marks)."""
    slices: dict[tuple[str, str], Slice] = field(default_factory=dict)
    inflight: dict[str, tuple[str, str, float]] = field(default_factory=dict)   # cid -> (module, symbol, signed notional)
    day_trades: deque = field(default_factory=lambda: deque(maxlen=500))         # (session_date, symbol)
    sectors: dict[str, str] = field(default_factory=dict)

    def slice(self, module_id: str, symbol: str) -> Slice:
        return self.slices.setdefault((module_id, symbol), Slice(module_id, symbol))

    def apply_fill(self, module_id: str, symbol: str, signed_qty: float, price: float, session: date) -> None:
        s = self.slice(module_id, symbol)
        prev, new = s.qty, s.qty + signed_qty
        if prev == 0 or (prev > 0) == (new > 0) and abs(new) > abs(prev):
            s.avg_price = (abs(prev) * s.avg_price + abs(signed_qty) * price) / abs(new) if abs(new) > 1e-12 else 0.0
        if prev == 0 and abs(new) > 1e-12:
            s.opened_session = session
        if abs(new) <= 1e-12:
            if s.opened_session == session:
                self.day_trades.append((session, symbol))
            s.qty, s.avg_price, s.opened_session, s.protective_order_id = 0.0, 0.0, None, None
            s.unprotected = s.unprotected_overnight = False
        else:
            s.qty = new

    def symbol_qty(self, symbol: str) -> float:
        return sum(s.qty for s in self.slices.values() if s.symbol == symbol)

    def open_positions(self) -> int:
        return len({s.symbol for s in self.slices.values() if abs(s.qty) > 1e-12})

    def gross(self, prices: dict[str, float]) -> float:
        return sum(abs(s.qty) * prices.get(s.symbol, s.avg_price) for s in self.slices.values()) + sum(abs(n) for _, _, n in self.inflight.values())

    def net(self, prices: dict[str, float]) -> float:
        return sum(s.qty * prices.get(s.symbol, s.avg_price) for s in self.slices.values()) + sum(n for _, _, n in self.inflight.values())

    def module_gross(self, module_id: str, prices: dict[str, float]) -> float:
        return sum(abs(s.qty) * prices.get(s.symbol, s.avg_price) for s in self.slices.values() if s.module_id == module_id) + \
            sum(abs(n) for m, _, n in self.inflight.values() if m == module_id)

    def sector_gross(self, sector: str, prices: dict[str, float]) -> float:
        return sum(abs(s.qty) * prices.get(s.symbol, s.avg_price) for s in self.slices.values() if self.sectors.get(s.symbol, "unknown") == sector)

    def day_trades_in_window(self, sessions: list[date]) -> int:
        w = set(sessions)
        return sum(1 for d, _ in self.day_trades if d in w)

    def snapshot(self, prices: dict[str, float]) -> dict[str, Any]:
        return {"gross": self.gross(prices), "net": self.net(prices), "open_positions": self.open_positions(),
                "slices": {f"{m}/{s}": round(sl.qty, 6) for (m, s), sl in self.slices.items() if abs(sl.qty) > 1e-12},
                "inflight": len(self.inflight)}


def limits_from_policy(policy: RiskPolicy, *, allow_fractional: bool = True, max_stale: int | None = None) -> RiskLimits:
    return RiskLimits(risk_per_trade_pct=policy.legacy_risk_pct, max_position_pct=policy.max_symbol_exposure_pct,
                      daily_loss_limit_pct=policy.max_daily_loss_pct, max_drawdown_pct=policy.max_drawdown_pct,
                      max_positions=policy.max_open_positions, atr_stop_mult=policy.legacy_atr_stop_mult, allow_fractional=allow_fractional,
                      max_spread_bps=max(policy.max_spread_bps.values()) if policy.max_spread_bps else 50.0,
                      max_stale_data_seconds=max_stale if max_stale is not None else policy.max_stale_seconds_auction)


class RiskEngine:
    def __init__(self, policy: RiskPolicy, *, safe: SafeLiveLimits | None = None, state: RiskState | None = None,
                 sectors: dict[str, str] | None = None, allow_fractional: bool = True, throttle_enabled: bool = True):
        self.policy = policy
        self.throttle_enabled = throttle_enabled
        self.manager = RiskManager(limits_from_policy(policy, allow_fractional=allow_fractional), state, safe=safe)
        self.ledger = ExposureLedger(sectors=dict(sectors or {}))
        self.throttles: dict[str, float] = {}          # module -> multiplier (<= 1.0), only ever lowered automatically
        self.recent_pnl: dict[str, deque] = {}
        self.halt_entries: bool = False
        self.halt_reason: str = ""

    # --------------------------------------------------------------- equity
    def update_equity(self, ts, equity: float):
        return self.manager.update_equity(pd.Timestamp(ts), equity)

    @property
    def killed(self) -> bool:
        return self.manager.killed

    # --------------------------------------------------------------- admission
    def admit(self, intent: TradeIntent, *, spread_bps: float | None, stale_seconds: float | None, is_etf: bool,
              account=None, recent_sessions: list[date] | None = None, shortable: bool = True) -> RiskDecision:
        """Pre-allocation admission of a strategy intent. Exits (direction 0) are always admitted."""
        P = self.policy
        checks = []

        def add(name, ok, detail=""):
            checks.append((name, bool(ok), detail))
        if intent.direction == 0:
            return RiskDecision(True, "APPROVED", "exit intent", ())
        add("module_allowed", P.module_allowed(intent.module_id), intent.module_id)
        add("symbol_allowed", not P.allowed_symbols or intent.symbol in P.allowed_symbols, intent.symbol)
        add("kill_switch_clear", not self.manager.killed, self.manager.state.kill_reason)
        add("daily_loss_ok", not self.manager.halted_today, "halted today")
        add("entries_not_halted", not self.halt_entries, self.halt_reason)
        add("short_allowed", intent.direction > 0 or (P.allow_short and shortable and (account is None or account.shorting_enabled)), f"dir={intent.direction}")
        add("overnight_allowed", (not intent.overnight_ok) or P.overnight_allowed(intent.module_id), f"overnight_ok={intent.overnight_ok}")
        cap = P.max_spread_bps.get("etf" if is_etf else "stock", 50.0)
        add("spread_ok", spread_bps is None or spread_bps <= cap, f"spread={spread_bps} cap={cap}")
        limit = P.max_stale_seconds_intraday if intent.module_id in INTRADAY_MODULES else P.max_stale_seconds_auction
        add("data_fresh", stale_seconds is None or stale_seconds <= limit, f"stale={stale_seconds} limit={limit}")
        if intent.module_id in INTRADAY_MODULES and P.pdt_mode == "legacy_guard":
            n = self.ledger.day_trades_in_window(recent_sessions or [])
            dt = account.daytrade_count if account is not None else 0
            add("pdt_legacy_guard", max(n, dt) < 3, f"day trades in 5 sessions: ledger={n} account={dt} (a 4th flags the account)")
        failed = [c for c in checks if not c[1]]
        from bot.risk.manager import RiskCheck
        tup = tuple(RiskCheck(n, ok, d) for n, ok, d in checks)
        if failed:
            return RiskDecision(False, failed[0][0], failed[0][2], tup)
        return RiskDecision(True, "APPROVED", "", tup)

    def budget_multiplier(self, module_id: str) -> float:
        return self.throttles.get(module_id, 1.0)

    # --------------------------------------------------------------- order gate
    def check(self, order: OrderIntent, **kw) -> RiskDecision:
        return self.manager.check_order(order, **kw)

    # --------------------------------------------------------------- throttle
    def record_trade_pnl(self, module_id: str, pnl: float, *, window: int = 60) -> str | None:
        dq = self.recent_pnl.setdefault(module_id, deque(maxlen=window))
        dq.append(pnl)
        if self.throttle_enabled and len(dq) == window and sum(dq) / window < 0 and self.throttles.get(module_id, 1.0) > 0.5:
            self.throttles[module_id] = 0.5
            log.warning("THROTTLE %s: last %d trades net negative; risk budget halved (operator must unthrottle)", module_id, window)
            return module_id
        return None

    def unthrottle(self, module_id: str) -> None:
        """Operator action only."""
        self.throttles.pop(module_id, None)
