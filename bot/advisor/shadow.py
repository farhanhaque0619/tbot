"""Shadow-mode recording and calibration measurement (Phase 10)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from bot.advisor.base import MarketAdvice
from bot.execution.market_state import MarketState


class ShadowRecorder:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, state: MarketState, advice: MarketAdvice, strategy_signal: int | None, session: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": state.timestamp, "session": session, "symbol": state.symbol, "state": state.to_dict(),
                                "advice": advice.to_dict(), "strategy_signal": strategy_signal}, default=str) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def calibration(records: list[dict[str, Any]], bars_by_symbol: dict[str, pd.DataFrame], horizon: int = 5) -> pd.DataFrame:
    """Join each advice with the realised forward return over ``horizon`` bars after its session.
    Returns per-(field, value) rows: n, mean forward return, hit rate of the implied direction. No thresholds are
    assumed; the operator judges whether any bucket is informative given n."""
    rows = []
    for r in records:
        sym, sess = r["symbol"], r.get("session")
        df = bars_by_symbol.get(sym)
        if df is None or not sess:
            continue
        ts = pd.Timestamp(sess, tz=df.index.tz)
        pos = df.index.searchsorted(ts, side="right")   # first bar strictly after the session
        if pos + horizon - 1 >= len(df) or pos == 0:
            continue
        fwd = float(df["close"].iloc[pos + horizon - 1] / df["close"].iloc[pos - 1] - 1)
        adv = r["advice"]
        rows.append({"symbol": sym, "regime": adv["regime"], "direction": adv["direction"], "risk_state": adv["risk_state"],
                     "setup_quality": adv["setup_quality"], "fwd_return": fwd, "strategy_signal": r.get("strategy_signal")})
    if not rows:
        return pd.DataFrame(columns=["field", "value", "n", "mean_fwd_return", "hit_rate"])
    df = pd.DataFrame(rows)
    out = []
    for field in ("direction", "regime", "risk_state", "setup_quality"):
        for val, g in df.groupby(field):
            if field == "direction":
                sign = {"long": 1, "short": -1}.get(val, 0)
                hit = float(((g["fwd_return"] * sign) > 0).mean()) if sign else float("nan")
            else:
                hit = float((g["fwd_return"] > 0).mean())
            out.append({"field": field, "value": val, "n": int(len(g)), "mean_fwd_return": float(g["fwd_return"].mean()), "hit_rate": hit})
    return pd.DataFrame(out)
