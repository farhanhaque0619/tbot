from bot.strategies.base import Bar, Signal, Strategy
from bot.strategies.ma_crossover import MACrossover
from bot.strategies.mean_reversion import MeanReversion

STRATEGIES: dict[str, type[Strategy]] = {
    MACrossover.name: MACrossover,
    MeanReversion.name: MeanReversion,
}


def get_strategy_class(name: str) -> type[Strategy]:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise SystemExit(f"unknown strategy '{name}'. Available: {', '.join(STRATEGIES)}") from None


__all__ = ["Bar", "Signal", "Strategy", "MACrossover", "MeanReversion", "STRATEGIES", "get_strategy_class"]
