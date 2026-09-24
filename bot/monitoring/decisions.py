"""Append-only decision records (Phase 14). One JSON line per (cycle, symbol). Never contains credentials."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DecisionRecord:
    timestamp: str
    environment: str
    strategy: str
    symbol: str
    session: str = ""
    market_state: dict[str, Any] | None = None
    signal: dict[str, Any] | None = None          # {"target": int, "reason": str}
    desired_position: float | None = None
    current_broker_position: float | None = None
    risk_decision: dict[str, Any] | None = None
    order_decision: str = "none"                  # none | submit | skip | blocked | waiting
    order_id: str | None = None
    client_order_id: str | None = None
    broker_request_id: str | None = None
    fill_result: dict[str, Any] | None = None
    realized_slippage_bps: float | None = None
    exception: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


class DecisionLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rec: DecisionRecord) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(rec.to_json() + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        out.append({"corrupt_line": line[:200]})
        return out
