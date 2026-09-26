"""Allocator (Phase 2/4, spec §6): intents -> target positions. Vol-normalised notional, caps in a fixed order that
scale only NEW intents pro rata, correlation haircut, netting per symbol, rounding. No optimiser, no Kelly. Every
scaling decision is logged on the target."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from bot.core.intents import TargetPosition, TradeIntent
from bot.core.policy import RiskPolicy
from bot.risk.sizing import round_qty

log = logging.getLogger(__name__)
AUCTION_STYLES = ("opg", "cls")


@dataclass(frozen=True)
class PositionView:
    symbol: str
    module_id: str
    qty: float                 # signed
    price: float

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass
class AllocationResult:
    targets: list[TargetPosition] = field(default_factory=list)
    netted_qty: dict[str, float] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)


class Allocator:
    def __init__(self, policy: RiskPolicy, *, sectors: dict[str, str] | None = None, whole_share_capable: bool | Callable[[str], bool] = False,
                 qty_decimals: int = 3):
        self.policy = policy
        self.sectors = dict(sectors or {})
        self._whole = whole_share_capable
        self.qty_decimals = qty_decimals

    def whole_share_capable(self, symbol: str) -> bool:
        return self._whole(symbol) if callable(self._whole) else bool(self._whole)

    # ------------------------------------------------------------------ main
    def allocate(self, intents: list[TradeIntent], positions: list[PositionView], equity: float, *, cash: float | None = None,
                 corr_matrix: pd.DataFrame | None = None) -> AllocationResult:
        P, res = self.policy, AllocationResult()
        if equity <= 0:
            res.log.append("equity <= 0: nothing allocated")
            return res
        # existing exposure by symbol / module / sector (absolute notionals)
        sym_exp: dict[str, float] = {}
        mod_exp: dict[str, float] = {}
        sec_exp: dict[str, float] = {}
        net_existing = 0.0
        for pv in positions:
            sym_exp[pv.symbol] = sym_exp.get(pv.symbol, 0.0) + abs(pv.notional)
            mod_exp[pv.module_id] = mod_exp.get(pv.module_id, 0.0) + abs(pv.notional)
            sec = self.sectors.get(pv.symbol, "unknown")
            sec_exp[sec] = sec_exp.get(sec, 0.0) + abs(pv.notional)
            net_existing += pv.notional
        gross_existing = sum(sym_exp.values())
        slice_qty = {(pv.module_id, pv.symbol): pv.qty for pv in positions}

        # 1. raw notionals (signed)
        raw: list[tuple[TradeIntent, float, list[str]]] = []
        for it in intents:
            notes: list[str] = []
            if it.direction == 0:
                raw.append((it, 0.0, ["exit: flat this module slice"]))
                continue
            if it.target_weight is not None:                       # M1 path: intent IS a target weight
                notional = max(0.0, min(it.target_weight, 1.0)) * equity
                notes.append(f"target_weight {it.target_weight:.3f} -> {notional:.2f}")
            else:
                if not (it.volatility and it.volatility > 0 and math.isfinite(it.volatility)):
                    notes.append("dropped: volatility <= 0")
                    raw.append((it, 0.0, notes))
                    continue
                notional = it.risk_budget_pct * equity / it.volatility
                notes.append(f"risk {it.risk_budget_pct:.4f} x equity / sigma {it.volatility:.4f} -> {notional:.2f}")
            raw.append((it, it.direction * notional, notes))

        # 2. caps, scaling only the new intents pro rata
        def scale_new(factor: float, why: str):
            if factor >= 1.0 - 1e-12:
                return
            for i, (it, n, notes) in enumerate(raw):
                if n != 0.0:
                    raw[i] = (it, n * factor, notes + [f"{why}: x{factor:.3f}"])
        # per-symbol
        for i, (it, n, notes) in enumerate(raw):
            if n == 0.0:
                continue
            cap = P.max_symbol_exposure_pct * equity - sym_exp.get(it.symbol, 0.0)
            existing_slice = slice_qty.get((it.module_id, it.symbol), 0.0) * it.reference_price
            cap += abs(existing_slice)   # this module's own slice is being replaced, not added
            if abs(n) > max(cap, 0.0):
                raw[i] = (it, math.copysign(max(cap, 0.0), n), notes + [f"symbol cap {P.max_symbol_exposure_pct:.0%}: {abs(n):.2f} -> {max(cap, 0.0):.2f}"])
        # per-module gross
        for mod in {it.module_id for it, n, _ in raw if n != 0.0}:
            new_mod = sum(abs(n) for it, n, _ in raw if it.module_id == mod)
            cap = P.module_gross_cap(mod) * equity - mod_exp.get(mod, 0.0)
            if new_mod > max(cap, 0.0) and new_mod > 0:
                f = max(cap, 0.0) / new_mod
                for i, (it, n, notes) in enumerate(raw):
                    if it.module_id == mod and n != 0.0:
                        raw[i] = (it, n * f, notes + [f"module {mod} gross cap: x{f:.3f}"])
        # sector cap (fraction of the portfolio gross cap)
        for sec in {self.sectors.get(it.symbol, "unknown") for it, n, _ in raw if n != 0.0}:
            new_sec = sum(abs(n) for it, n, _ in raw if self.sectors.get(it.symbol, "unknown") == sec)
            cap = P.max_sector_pct_of_gross * P.max_gross_pct * equity - sec_exp.get(sec, 0.0)
            if new_sec > max(cap, 0.0) and new_sec > 0:
                f = max(cap, 0.0) / new_sec
                for i, (it, n, notes) in enumerate(raw):
                    if self.sectors.get(it.symbol, "unknown") == sec and n != 0.0:
                        raw[i] = (it, n * f, notes + [f"sector {sec} cap: x{f:.3f}"])
        # portfolio gross
        new_gross = sum(abs(n) for _, n, _ in raw)
        cap = P.max_gross_pct * equity - gross_existing
        if new_gross > max(cap, 0.0) and new_gross > 0:
            scale_new(max(cap, 0.0) / new_gross, "portfolio gross cap")
        # net bounds
        new_net = sum(n for _, n, _ in raw)
        if new_net > 0 and net_existing + new_net > P.max_net_pct * equity:
            room = max(P.max_net_pct * equity - net_existing, 0.0)
            scale_new(room / new_net, "max net")
        elif new_net < 0 and net_existing + new_net < P.min_net_pct * equity:
            room = max(net_existing - P.min_net_pct * equity, 0.0)
            scale_new(room / abs(new_net), "min net")
        # cash (no leverage) unless margin is allowed
        if not P.allow_margin and cash is not None:
            new_long = sum(n for _, n, _ in raw if n > 0)
            if new_long > max(cash, 0.0) and new_long > 0:
                f = max(cash, 0.0) / new_long
                for i, (it, n, notes) in enumerate(raw):
                    if n > 0:
                        raw[i] = (it, n * f, notes + [f"cash cap: x{f:.3f}"])
        # 3. correlation haircut
        active = [it.symbol for it, n, _ in raw if n != 0.0]
        if corr_matrix is not None and len(set(active)) > 1:
            syms = [s for s in set(active) if s in corr_matrix.index]
            if len(syms) > 1:
                sub = corr_matrix.loc[syms, syms].to_numpy()
                m = (sub.sum() - len(syms)) / (len(syms) * (len(syms) - 1))
                if m > P.correlation_haircut_threshold:
                    scale_new(P.correlation_haircut, f"correlation haircut (mean pairwise {m:.2f})")

        # 4./5. per-module targets with rounding, then netting per symbol
        for it, n, notes in raw:
            if it.direction == 0:
                res.targets.append(TargetPosition(it.symbol, it.module_id, 0.0, 0.0, it.reference_price, it.entry_style, it.exit_style,
                                                  it.protective_stop_price, it.overnight_ok, it, tuple(notes), kind="exit"))
                continue
            qty_raw = abs(n) / it.reference_price if it.reference_price > 0 else 0.0
            whole = self.whole_share_capable(it.symbol)
            if qty_raw >= 1.0 and (whole or it.entry_style in AUCTION_STYLES):
                qty = math.floor(qty_raw)
                notes = notes + ["rounded to whole shares"]
            else:
                qty = round_qty(qty_raw, fractional=not whole, decimals=self.qty_decimals)
                if not whole:
                    notes = notes + [f"fractional to {self.qty_decimals} dp"]
            notional = qty * it.reference_price
            if notional < P.min_notional or qty <= 0:
                notes = notes + [f"dropped: notional {notional:.2f} < min {P.min_notional:.2f}"]
                res.log.extend(f"{it.module_id}/{it.symbol}: {x}" for x in notes)
                continue
            res.targets.append(TargetPosition(it.symbol, it.module_id, it.direction * qty, notional, it.reference_price, it.entry_style,
                                              it.exit_style, it.protective_stop_price, it.overnight_ok, it, tuple(notes), kind="entry"))
        for t in res.targets:
            res.netted_qty[t.symbol] = res.netted_qty.get(t.symbol, 0.0) + t.target_qty
            res.log.extend(f"{t.module_id}/{t.symbol}: {x}" for x in t.scaling_log)
        return res
