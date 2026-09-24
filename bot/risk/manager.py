"""Portfolio-level risk limits shared by the backtester and the live/paper loop.

Two layers:

1. Stateful limits (``update_equity`` / ``can_open`` / ``position_qty``): daily loss halt, drawdown kill switch,
   max concurrent positions, fixed-fractional sizing. Used identically by the backtester and the trader.

2. ``check_order`` (Phase 13): a deterministic pre-trade gate run before EVERY order submission in execution.
   It returns ``RiskDecision(approved, code, detail, checks)``. There is no fuzzy approval; nothing above this
   layer (strategy, advisor, operator flag) can bypass it. ``SafeLiveLimits`` adds the Phase 5 hard caps for a
   tiny live account and can only be changed by editing configuration - never by the bot.

State is a plain dataclass so it can be persisted and restored across restarts.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from bot.risk.sizing import fixed_fractional_qty


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.50
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.20
    max_positions: int = 5
    atr_stop_mult: float = 2.0
    allow_fractional: bool = False
    qty_decimals: int = 3
    max_spread_bps: float = 50.0
    max_price_deviation_pct: float = 0.10
    max_stale_data_seconds: int = 900
    max_realized_vol: float | None = None   # optional volatility sanity gate (annualised)

    @classmethod
    def from_settings(cls, s) -> RiskLimits:
        return cls(s.risk_per_trade_pct, s.max_position_pct, s.daily_loss_limit_pct, s.max_drawdown_pct,
                   s.max_positions, s.atr_stop_mult, s.allow_fractional, s.qty_decimals, s.max_spread_bps,
                   s.max_price_deviation_pct, s.max_stale_data_seconds)


@dataclass(frozen=True)
class SafeLiveLimits:
    """Hard caps for SAFE_LIVE_TEST_MODE. Dollar amounts, deterministic, operator-owned."""
    max_order_notional: float = 25.0
    max_gross_exposure: float = 50.0
    max_daily_loss: float = 5.0
    max_account_drawdown: float = 10.0
    max_positions: int = 1
    allowed_symbols: tuple[str, ...] = ()
    allow_short: bool = False
    allow_margin: bool = False
    allow_extended_hours: bool = False
    fractionable_only: bool = True
    allow_pyramiding: bool = False

    @classmethod
    def from_settings(cls, s) -> SafeLiveLimits:
        syms = tuple(x.strip().upper() for x in s.safe_allowed_symbols.split(",") if x.strip())
        return cls(s.safe_max_order_notional, s.safe_max_gross_exposure, s.safe_max_daily_loss,
                   s.safe_max_account_drawdown, s.safe_max_positions, syms)

    def summary(self) -> list[tuple[str, str]]:
        return [("max order notional", f"${self.max_order_notional:,.2f}"),
                ("max gross exposure", f"${self.max_gross_exposure:,.2f}"),
                ("max daily loss", f"${self.max_daily_loss:,.2f}"),
                ("max account drawdown", f"${self.max_account_drawdown:,.2f} from peak"),
                ("max open positions", str(self.max_positions)),
                ("allowed symbols", ", ".join(self.allowed_symbols) or "any fractionable US equity"),
                ("shorting", "no"), ("margin / borrowing", "no"), ("extended hours", "no"),
                ("options / crypto", "no (not implemented)"), ("pyramiding / averaging down", "no"),
                ("Kelly / model sizing", "no")]

    def fingerprint(self) -> str:
        import hashlib
        return hashlib.sha256(repr(asdict(self)).encode()).hexdigest()[:16]


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str | None = None            # ISO date of the current trading day
    day_start_equity: float = 0.0
    last_equity: float = 0.0
    halted_today: bool = False
    killed: bool = False
    kill_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RiskState:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class RiskEvent:
    kind: str          # "daily_halt" | "kill_switch" | "new_day"
    ts: pd.Timestamp
    equity: float
    detail: str = ""


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str                    # "buy" | "sell"
    qty: float
    kind: str                    # "entry" | "exit"
    reference_price: float       # price used for sizing (last close)
    client_order_id: str
    tif: str = "day"
    reason: str = ""

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.reference_price

    @property
    def is_fractional(self) -> bool:
        return abs(self.qty - round(self.qty)) > 1e-9


@dataclass(frozen=True)
class RiskCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    code: str                    # "APPROVED" or the name of the first failed check
    detail: str
    checks: tuple[RiskCheck, ...] = ()

    @property
    def failed(self) -> list[RiskCheck]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "code": self.code, "detail": self.detail,
                "failed": [c.name for c in self.failed]}


class RiskManager:
    def __init__(self, limits: RiskLimits, state: RiskState | None = None, safe: SafeLiveLimits | None = None):
        self.limits = limits
        self.state = state or RiskState()
        self.safe = safe

    # ---------------------------------------------------------------- equity
    def update_equity(self, ts: pd.Timestamp, equity: float) -> list[RiskEvent]:
        """Feed a new equity mark. Returns any limit events triggered by this mark."""
        s, L = self.state, self.limits
        events: list[RiskEvent] = []
        d = pd.Timestamp(ts).date().isoformat()
        if s.day != d:
            s.day = d
            s.day_start_equity = s.last_equity if s.last_equity > 0 else equity
            s.halted_today = False
            events.append(RiskEvent("new_day", ts, equity))
        s.last_equity = equity
        if equity > s.peak_equity:
            s.peak_equity = equity
        if not s.killed and s.peak_equity > 0:
            dd = 1 - equity / s.peak_equity
            dd_dollars = s.peak_equity - equity
            if dd >= L.max_drawdown_pct:
                s.killed, s.kill_reason = True, f"drawdown {dd:.1%} >= limit {L.max_drawdown_pct:.1%} (peak {s.peak_equity:.2f})"
            elif self.safe is not None and dd_dollars >= self.safe.max_account_drawdown:
                s.killed, s.kill_reason = True, f"SAFE mode: drawdown ${dd_dollars:.2f} >= ${self.safe.max_account_drawdown:.2f} (peak {s.peak_equity:.2f})"
            if s.killed:
                events.append(RiskEvent("kill_switch", ts, equity, s.kill_reason))
        if not s.halted_today and s.day_start_equity > 0:
            day_ret = equity / s.day_start_equity - 1
            day_loss = s.day_start_equity - equity
            if day_ret <= -L.daily_loss_limit_pct:
                s.halted_today = True
                events.append(RiskEvent("daily_halt", ts, equity, f"day P&L {day_ret:.2%} <= -{L.daily_loss_limit_pct:.1%}"))
            elif self.safe is not None and day_loss >= self.safe.max_daily_loss:
                s.halted_today = True
                events.append(RiskEvent("daily_halt", ts, equity, f"SAFE mode: day loss ${day_loss:.2f} >= ${self.safe.max_daily_loss:.2f}"))
        return events

    # ---------------------------------------------------------------- gates
    @property
    def killed(self) -> bool:
        return self.state.killed

    @property
    def halted_today(self) -> bool:
        return self.state.halted_today

    def can_open(self, n_open_positions: int) -> tuple[bool, str]:
        if self.state.killed:
            return False, "kill switch active"
        if self.state.halted_today:
            return False, "daily loss limit hit"
        cap = self.limits.max_positions if self.safe is None else min(self.limits.max_positions, self.safe.max_positions)
        if n_open_positions >= cap:
            return False, f"max positions ({cap}) reached"
        return True, ""

    def reset_kill_switch(self) -> None:
        """Manual, deliberate human action only."""
        self.state.killed = False
        self.state.kill_reason = ""
        self.state.peak_equity = self.state.last_equity

    # --------------------------------------------------------------- sizing
    def stop_distance(self, atr_value: float | None, price: float) -> float:
        if atr_value and atr_value > 0:
            return self.limits.atr_stop_mult * atr_value
        return 0.02 * price  # conservative default when ATR isn't ready

    def position_qty(self, equity: float, price: float, stop_distance: float, cash_available: float | None = None) -> float:
        max_notional_cap = self.safe.max_order_notional if self.safe is not None else None
        return fixed_fractional_qty(equity, price, stop_distance, risk_pct=self.limits.risk_per_trade_pct,
                                    max_position_pct=self.limits.max_position_pct, cash_available=cash_available,
                                    fractional=self.limits.allow_fractional, decimals=self.limits.qty_decimals,
                                    max_notional=max_notional_cap)

    # ------------------------------------------------------- pre-trade gate
    def check_order(self, intent: OrderIntent, *, state, account, asset=None, broker_env: str = "paper",
                    expected_env: str = "paper", account_is_paper_shaped: bool | None = None,
                    open_orders=(), known_client_ids=(), position_qty: float = 0.0, n_positions: int = 0,
                    tif: str = "day", now: datetime | None = None, bar_current: bool = True) -> RiskDecision:
        """Deterministic pre-trade checks. Exits (risk-reducing) skip the entry-only limits but never the
        integrity checks (env, account, duplicates, conflicts)."""
        L, S = self.limits, self.safe
        is_exit = intent.kind == "exit"
        checks: list[RiskCheck] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append(RiskCheck(name, bool(ok), detail))

        # --- integrity (apply to everything) ---
        add("account_healthy", account is not None and account.healthy,
            "" if account is None else f"status={account.status} trading_blocked={account.trading_blocked} account_blocked={account.account_blocked}")
        add("mode_consistent", broker_env == expected_env and (account_is_paper_shaped is None or account_is_paper_shaped == (expected_env == "paper")),
            f"broker_env={broker_env} expected={expected_env} account_paper_shaped={account_is_paper_shaped}")
        add("no_duplicate_order", intent.client_order_id not in set(known_client_ids), intent.client_order_id)
        add("no_outstanding_order_conflict", not any(o.symbol == intent.symbol and o.is_open for o in open_orders),
            f"open orders for {intent.symbol}: {sum(1 for o in open_orders if o.symbol == intent.symbol and o.is_open)}")
        add("qty_positive", intent.qty > 0, f"qty={intent.qty}")
        add("price_sane", math.isfinite(intent.reference_price) and intent.reference_price > 0
            and (not math.isfinite(state.mid) or state.mid <= 0 or abs(state.mid / intent.reference_price - 1) <= L.max_price_deviation_pct),
            f"ref={intent.reference_price:.4f} mid={state.mid}")
        add("fractionable_if_needed", (not intent.is_fractional) or (asset is not None and asset.fractionable),
            f"fractional={intent.is_fractional} asset_fractionable={getattr(asset, 'fractionable', None)}")
        add("fractional_tif_day", (not intent.is_fractional) or tif == "day", f"tif={tif}")
        add("symbol_tradable", asset is None or (asset.tradable and asset.asset_class == "us_equity"),
            f"tradable={getattr(asset, 'tradable', None)} class={getattr(asset, 'asset_class', None)}")
        if is_exit:
            add("strategy_state_valid", position_qty != 0 and abs(intent.qty) <= abs(position_qty) + 1e-9
                and ((position_qty > 0 and intent.side == "sell") or (position_qty < 0 and intent.side == "buy")),
                f"exit {intent.side} {intent.qty} vs position {position_qty}")
        else:
            # --- entry-only limits ---
            add("strategy_state_valid", position_qty == 0 or S is None or S.allow_pyramiding,
                f"entry while position={position_qty}")
            add("kill_switch_clear", not self.state.killed, self.state.kill_reason)
            add("daily_loss_ok", not self.state.halted_today, "daily loss limit hit")
            # OPG orders are placed after hours (quotes are necessarily hours old); the completed daily bar is the
            # relevant data. DAY orders need a quote no older than the configured limit.
            quote_fresh = math.isfinite(state.stale_data_seconds) and state.stale_data_seconds <= L.max_stale_data_seconds
            add("data_fresh", bar_current and (tif == "opg" or quote_fresh),
                f"bar_current={bar_current} stale={state.stale_data_seconds:.0f}s limit={L.max_stale_data_seconds}s tif={tif}")
            add("market_permitted", state.market_open or tif == "opg", f"market_open={state.market_open} tif={tif}")
            add("spread_sane", not math.isfinite(state.spread_bps) or state.spread_bps <= L.max_spread_bps,
                f"spread={state.spread_bps:.1f}bps limit={L.max_spread_bps}")
            if L.max_realized_vol is not None:
                add("volatility_sane", not math.isfinite(state.realized_vol) or state.realized_vol <= L.max_realized_vol,
                    f"rvol={state.realized_vol:.2f}")
            cap = L.max_positions if S is None else min(L.max_positions, S.max_positions)
            add("position_limit", n_positions < cap, f"open={n_positions} cap={cap}")
            eq = account.equity if account else 0.0
            add("order_notional", intent.notional <= L.max_position_pct * eq + 1e-6,
                f"notional={intent.notional:.2f} cap={L.max_position_pct * eq:.2f}")
            bp = (account.cash if (S is not None and not S.allow_margin) else account.buying_power) if account else 0.0
            add("buying_power", intent.side != "buy" or intent.notional <= bp + 1e-6, f"notional={intent.notional:.2f} available={bp:.2f}")
            add("no_short_unless_allowed", intent.side == "buy" or (S is None or S.allow_short) and (account is not None and account.shorting_enabled),
                f"side={intent.side}")
            if S is not None:
                add("safe_symbol_allowed", not S.allowed_symbols or intent.symbol.upper() in S.allowed_symbols, intent.symbol)
                add("safe_fractionable_only", not S.fractionable_only or (asset is not None and asset.fractionable), "")
                add("safe_order_notional", intent.notional <= S.max_order_notional + 1e-6,
                    f"notional={intent.notional:.2f} cap={S.max_order_notional:.2f}")
                add("safe_gross_exposure", state.gross_exposure + intent.notional <= S.max_gross_exposure + 1e-6,
                    f"gross={state.gross_exposure:.2f}+{intent.notional:.2f} cap={S.max_gross_exposure:.2f}")
                add("safe_daily_loss", state.daily_pnl > -S.max_daily_loss, f"day_pnl={state.daily_pnl:.2f} cap=-{S.max_daily_loss:.2f}")
                add("safe_drawdown", (self.state.peak_equity - (account.equity if account else 0.0)) < S.max_account_drawdown,
                    f"dd=${self.state.peak_equity - (account.equity if account else 0.0):.2f} cap=${S.max_account_drawdown:.2f}")
                add("safe_no_extended_hours", tif in ("day", "opg"), f"tif={tif}")
        failed = [c for c in checks if not c.ok]
        if failed:
            return RiskDecision(False, failed[0].name, failed[0].detail, tuple(checks))
        return RiskDecision(True, "APPROVED", "", tuple(checks))
