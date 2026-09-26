"""Universe configuration and Tier 3 builder (Phase 1)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import pandas as pd

import yaml

log = logging.getLogger(__name__)


@dataclass
class Universe:
    tier1: list[str]
    tier2: list[str]
    tier3: list[str]
    sectors: dict[str, str]
    plan_cap: int = 30
    tier3_built_on: str | None = None
    path: Path | None = None

    @property
    def symbols(self) -> list[str]:
        seen, out = set(), []
        for s in self.tier1 + self.tier2 + self.tier3:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def sector_of(self, symbol: str) -> str:
        return self.sectors.get(symbol.upper(), "unknown")

    def check_cap(self, data_plan: str) -> None:
        n = len(self.symbols)
        if data_plan == "basic" and n > self.plan_cap:
            raise ValueError(f"universe has {n} symbols; the Basic data plan streams at most {self.plan_cap}. "
                             f"Trim the universe or set DATA_PLAN=plus.")

    @classmethod
    def load(cls, path: Path | str = "config/universe.yaml") -> Universe:
        p = Path(path)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        tiers = raw.get("tiers", {})
        return cls(tier1=[s.upper() for s in tiers.get("tier1", {}).get("symbols", [])],
                   tier2=[s.upper() for s in tiers.get("tier2", {}).get("symbols", [])],
                   tier3=[s.upper() for s in tiers.get("tier3", {}).get("symbols", []) or []],
                   sectors={k.upper(): v for k, v in (raw.get("sectors") or {}).items()},
                   plan_cap=int(raw.get("plan_cap", 30)), tier3_built_on=tiers.get("tier3", {}).get("built_on"), path=p)

    def save(self, path: Path | str | None = None) -> Path:
        p = Path(path or self.path or "config/universe.yaml")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        raw.setdefault("tiers", {}).setdefault("tier3", {})
        raw["tiers"]["tier3"]["symbols"] = list(self.tier3)
        raw["tiers"]["tier3"]["built_on"] = self.tier3_built_on
        raw["sectors"] = {**(raw.get("sectors") or {}), **self.sectors}
        raw["plan_cap"] = self.plan_cap
        p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return p


@dataclass
class Candidate:
    symbol: str
    sector: str
    price: float = 0.0
    median_dollar_volume: float = 0.0
    median_spread_bps: float = float("inf")
    fractionable: bool = False
    tradable: bool = False
    easy_to_borrow: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.reasons


def read_candidates(path: Path | str = "config/tier3_candidates.txt") -> list[tuple[str, str]]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sym, _, sector = line.partition(",")
        out.append((sym.strip().upper(), sector.strip() or "unknown"))
    return out


def build_tier3(candidates: list[tuple[str, str]], *, daily_bars: Callable[[str, date, date], "pd.DataFrame"],
                asset_info: Callable[[str], object], spread_samples: Callable[[str], list[float]], asof: date,
                n: int = 15, min_price: float = 20.0, max_spread_bps: float = 5.0, lookback_days: int = 60) -> tuple[list[Candidate], list[Candidate]]:
    """Rank candidates by 60-session median dollar volume subject to the eligibility filters. Returns
    (selected, all_evaluated). Every exclusion reason is recorded so the operator can audit the list."""
    evaluated: list[Candidate] = []
    for sym, sector in candidates:
        c = Candidate(sym, sector)
        try:
            a = asset_info(sym)
            c.fractionable, c.tradable, c.easy_to_borrow = bool(a.fractionable), bool(a.tradable), bool(a.easy_to_borrow)
        except Exception as e:  # noqa: BLE001
            c.reasons.append(f"asset lookup failed: {type(e).__name__}")
            evaluated.append(c)
            continue
        for flag, name in ((c.fractionable, "not fractionable"), (c.tradable, "not tradable"), (c.easy_to_borrow, "not easy to borrow")):
            if not flag:
                c.reasons.append(name)
        try:
            bars = daily_bars(sym, asof - timedelta(days=int(lookback_days * 1.6) + 5), asof).tail(lookback_days)
            if len(bars) < lookback_days * 0.8:
                c.reasons.append(f"only {len(bars)} daily bars")
            else:
                c.price = float(bars["close"].iloc[-1])
                c.median_dollar_volume = float((bars["close"] * bars["volume"]).median())
                if c.price < min_price:
                    c.reasons.append(f"price {c.price:.2f} < {min_price}")
        except Exception as e:  # noqa: BLE001
            c.reasons.append(f"bars failed: {type(e).__name__}")
        try:
            samples = [x for x in spread_samples(sym) if x == x and x >= 0]
            c.median_spread_bps = median(samples) if samples else float("inf")
            if c.median_spread_bps > max_spread_bps:
                c.reasons.append(f"spread {c.median_spread_bps:.1f} bps > {max_spread_bps}")
        except Exception as e:  # noqa: BLE001
            c.reasons.append(f"spread sampling failed: {type(e).__name__}")
        evaluated.append(c)
    eligible = sorted([c for c in evaluated if c.eligible], key=lambda c: -c.median_dollar_volume)
    return eligible[:n], evaluated
