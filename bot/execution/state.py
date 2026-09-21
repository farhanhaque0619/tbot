"""Persistent bot state (JSON, atomic writes) so a restart never double-orders.

What is stored:
- ``last_processed``: per symbol, the last completed bar date the strategy acted on.
- ``orders``: every order we ever submitted, keyed by client_order_id, with status.
- ``positions``: our view of open positions (entry, stop) - reconciled against the broker.
- ``risk``: RiskState (peak equity, kill switch, daily halt).
- ``risk_exits``: per symbol, dates on which the risk layer forced an exit
  (replayed into the strategy so its state matches reality after a restart).
- ``trades``: closed round trips for the dashboard.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BotState:
    run_id: str = "paper"
    strategy: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    last_processed: dict[str, str] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    risk_exits: dict[str, list[str]] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_log: list[dict[str, Any]] = field(default_factory=list)
    last_cycle: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BotState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class StateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        with self.path.open("r", encoding="utf-8") as f:
            return BotState.from_dict(json.load(f))

    def save(self, state: BotState) -> None:
        """Write to a temp file in the same directory, fsync, then atomically rename."""
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
