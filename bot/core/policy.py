"""RiskPolicy: operator-owned, frozen, fingerprinted (Phase 2/4).

Nothing in the process may raise a value in a loaded policy; the only automatic change is a throttle (tightening),
recorded separately. `fingerprint()` is a SHA-256 of the canonical JSON; the arm flow stores it and the daemon
refuses to run when the loaded policy's fingerprint differs from the armed one.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PdtMode = Literal["legacy_guard", "intraday_margin", "none"]
LEGACY_MODULES = ("ma_crossover", "mean_reversion")      # V1 baselines: gated by FINAL_REPORT.md, not by the V1.5 protocol
PROMOTIONS_PATH = Path("research/PROMOTIONS.md")


def promoted_modules(path: str | Path = PROMOTIONS_PATH) -> set[str]:
    """Modules with a promotion record: a line ``## <MODULE> promoted <YYYY-MM-DD>`` in research/PROMOTIONS.md."""
    p = Path(path)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.startswith("## "):          # an indented example inside a code block is not a record
            continue
        parts = line.split()
        if len(parts) >= 4 and parts[1].isidentifier() and parts[2].lower() == "promoted":
            out.add(parts[1])
    return out


class RiskPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "paper"
    allowed_modules: tuple[str, ...] = ("ma_crossover", "mean_reversion")
    allowed_symbols: tuple[str, ...] = ()                 # empty = universe decides
    max_order_notional_pct: float = Field(0.50, gt=0, le=2.0)
    max_symbol_exposure_pct: float = Field(0.50, gt=0, le=2.0)
    max_module_gross_pct: dict[str, float] = Field(default_factory=dict)
    max_gross_pct: float = Field(1.0, gt=0, le=3.0)
    max_net_pct: float = Field(1.0, le=3.0)
    min_net_pct: float = Field(0.0, ge=-3.0)
    max_sector_pct_of_gross: float = Field(0.30, gt=0, le=1.0)
    max_daily_loss_pct: float = Field(0.03, gt=0, le=0.5)
    max_drawdown_pct: float = Field(0.20, gt=0, le=0.9)
    max_open_positions: int = Field(5, ge=1)
    allow_short: bool = False
    allow_margin: bool = False
    allow_overnight: dict[str, bool] = Field(default_factory=dict)   # per module; default False if absent
    allow_extended_hours: bool = False
    max_spread_bps: dict[str, float] = Field(default_factory=lambda: {"etf": 5.0, "stock": 10.0})
    max_stale_seconds_intraday: int = Field(90, ge=1)
    max_stale_seconds_auction: int = Field(900, ge=1)
    pdt_mode: PdtMode = "legacy_guard"
    require_broker_protection_overnight: bool = True
    correlation_haircut_threshold: float = Field(0.5, ge=0, le=1)
    correlation_haircut: float = Field(0.7, gt=0, le=1)
    legacy_atr_stop: bool = True
    legacy_risk_pct: float = Field(0.01, gt=0, le=0.1)
    legacy_atr_stop_mult: float = Field(2.0, gt=0)
    legacy_max_position_pct: float = Field(0.50, gt=0, le=1.0)
    min_notional: float = Field(1.0, ge=0)
    orphan_policy: Literal["adopt", "flatten"] = "adopt"    # broker positions nobody owns: adopt as module "orphan" (with protection) or flatten

    # ------------------------------------------------------------------ helpers
    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def module_allowed(self, module_id: str) -> bool:
        return module_id in self.allowed_modules

    def overnight_allowed(self, module_id: str) -> bool:
        return bool(self.allow_overnight.get(module_id, False))

    def module_gross_cap(self, module_id: str) -> float:
        return float(self.max_module_gross_pct.get(module_id, self.max_gross_pct))

    def unpromoted_modules(self, promotions_path: str | Path = PROMOTIONS_PATH) -> list[str]:
        """Allowed V1.5 modules without a promotion record (spec §11: none of these may trade live)."""
        promoted = promoted_modules(promotions_path)
        return [m for m in self.allowed_modules if m not in LEGACY_MODULES and m not in promoted]

    @classmethod
    def load(cls, path: str | Path, *, require_promotions: bool = False, promotions_path: str | Path = PROMOTIONS_PATH) -> RiskPolicy:
        """Load a policy file. With ``require_promotions=True`` (the live path) refuse a policy that allows a V1.5 module
        without an entry in research/PROMOTIONS.md."""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for k in ("allowed_modules", "allowed_symbols"):
            if k in raw and isinstance(raw[k], list):
                raw[k] = tuple(raw[k])
        pol = cls(**raw)
        if require_promotions:
            bad = pol.unpromoted_modules(promotions_path)
            if bad:
                raise ValueError(f"policy {path} allows unpromoted modules {bad}: no promotion record in {promotions_path} "
                                 f"(research protocol §11); remove them from allowed_modules or complete the protocol")
        return pol

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        d = self.model_dump(mode="json")
        d["allowed_modules"], d["allowed_symbols"] = list(d["allowed_modules"]), list(d["allowed_symbols"])
        p.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
        return p


def legacy_policy(**overrides) -> RiskPolicy:
    """Policy equivalent to V1's RiskLimits defaults, used for baseline reproduction."""
    base = dict(name="legacy", allowed_modules=("ma_crossover", "mean_reversion"), max_symbol_exposure_pct=0.50,
                max_gross_pct=1.0, max_sector_pct_of_gross=1.0, max_daily_loss_pct=0.03, max_drawdown_pct=0.20, max_open_positions=5,
                allow_overnight={"ma_crossover": True, "mean_reversion": True}, legacy_atr_stop=True, legacy_risk_pct=0.01,
                legacy_atr_stop_mult=2.0, legacy_max_position_pct=0.50, pdt_mode="none", require_broker_protection_overnight=False)
    base.update(overrides)
    return RiskPolicy(**base)
