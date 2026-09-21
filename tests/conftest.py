import numpy as np
import pandas as pd
import pytest

NY = "America/New_York"


def make_bars(n: int = 600, seed: int = 0, start: str = "2020-01-01", drift: float = 0.0003, vol: float = 0.01,
              closes=None) -> pd.DataFrame:
    """Synthetic daily OHLCV, tz-aware NY index, business days."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, tz=NY)
    if closes is None:
        closes = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    closes = np.asarray(closes, dtype=float)
    opens = closes * (1 + rng.normal(0, 0.002, n))
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.003, n)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.003, n)))
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1e6}, index=idx)


@pytest.fixture
def bars() -> pd.DataFrame:
    return make_bars()
