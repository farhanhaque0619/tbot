"""Kelly sizing - RESEARCH ONLY (Phase 11). ENABLE_KELLY defaults to false and the execution layer never imports
this module (enforced by tests/test_architecture.py). Kelly needs a trustworthy win-probability and payoff
distribution; an uncalibrated estimate oversizes catastrophically."""
from __future__ import annotations


def kelly_fraction(p_win: float, payoff_ratio: float) -> float:
    """Full Kelly for a binary bet: f* = p - (1-p)/b. Returns 0 when the edge is non-positive."""
    if not (0 <= p_win <= 1) or payoff_ratio <= 0:
        return 0.0
    f = p_win - (1 - p_win) / payoff_ratio
    return max(f, 0.0)


def fractional_kelly(p_win: float, payoff_ratio: float, fraction: float = 0.25, cap: float = 0.10) -> float:
    """Fraction of Kelly, capped. Even in research, never suggest more than ``cap`` of equity."""
    return min(kelly_fraction(p_win, payoff_ratio) * fraction, cap)


def kelly_from_trades(pnls: list[float], fraction: float = 0.25) -> dict[str, float]:
    wins = [x for x in pnls if x > 0]
    losses = [-x for x in pnls if x <= 0]
    if not wins or not losses or len(pnls) < 30:
        return {"p_win": 0.0, "payoff": 0.0, "full_kelly": 0.0, "fractional": 0.0, "n": len(pnls), "note": "insufficient trades (<30) or one-sided"}
    p = len(wins) / len(pnls)
    b = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    return {"p_win": p, "payoff": b, "full_kelly": kelly_fraction(p, b), "fractional": fractional_kelly(p, b, fraction), "n": len(pnls),
            "note": "point estimate from historical trades; not a calibrated forward probability"}
