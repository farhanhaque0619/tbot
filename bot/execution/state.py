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
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BotState:
    run_id: str = "paper"
    env: str = ""
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
    recovered_from_corruption: bool = False

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
        """Load state. A corrupt file is moved aside (never silently overwritten) and an empty state is returned;
        the next cycle's broker reconciliation re-adopts real positions, and ``last_error`` records the incident."""
        if not self.path.exists():
            return BotState()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state root is not an object")
            return BotState.from_dict(data)
        except (ValueError, TypeError) as e:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            os.replace(self.path, backup)
            log.error("state file %s is corrupt (%s); moved to %s and starting from an empty state. "
                      "Broker positions will be re-adopted on the next cycle.", self.path, e, backup)
            st = BotState()
            st.last_error = f"state file corrupt: {e}; backup at {backup.name}"
            st.recovered_from_corruption = True
            return st

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
