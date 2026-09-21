"""Cache-aware bar loading.

``BarLoader.get_daily`` returns bars for [start, end] plus ``warmup`` extra bars
before ``start`` (strategies need history before they can trade). It only calls
the provider for date ranges the cache does not already cover.

Split handling: prices are requested split-adjusted. When a new split happens,
the *whole* adjusted history changes, so cached bars go stale. We detect this by
re-fetching a small overlap at the tail and comparing: if the overlapping bars
disagree by more than 0.5%, the symbol's cache is discarded and refetched.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from bot.data.providers import BarProvider
from bot.data.store import BarStore

log = logging.getLogger(__name__)

OVERLAP_BARS = 5
SPLIT_TOLERANCE = 0.005


class BarLoader:
    def __init__(self, store: BarStore, provider: BarProvider | None, *, adjustment: str = "split"):
        self.store = store
        self.provider = provider
        self.adjustment = adjustment

    def get_daily(self, symbol: str, start: date, end: date, *, warmup: int = 0, refresh: bool = False) -> pd.DataFrame:
        symbol = symbol.upper()
        # ~1.6 calendar days per trading day, plus slack.
        fetch_start = start - timedelta(days=int(warmup * 1.6) + 10) if warmup else start
        if refresh:
            self.store.delete_symbol(symbol, adjustment=self.adjustment)
        self._ensure_cached(symbol, fetch_start, end)
        df = self.store.get_bars(symbol, fetch_start, end, adjustment=self.adjustment)
        if warmup:
            before = df.loc[: pd.Timestamp(start, tz="America/New_York") - pd.Timedelta(seconds=1)]
            after = df.loc[pd.Timestamp(start, tz="America/New_York"):]
            df = pd.concat([before.tail(warmup), after])
        return df

    # ------------------------------------------------------------------ cache
    def _ensure_cached(self, symbol: str, start: date, end: date) -> None:
        cov = self.store.get_coverage(symbol, adjustment=self.adjustment)
        if cov and cov[0] <= start and cov[1] >= end:
            return
        if self.provider is None:
            if cov:
                log.warning("%s: cache covers %s..%s but %s..%s requested and no provider configured",
                            symbol, cov[0], cov[1], start, end)
                return
            raise RuntimeError(f"{symbol}: no cached bars and no data provider configured "
                               f"(set Alpaca keys or import a CSV with `python -m bot data import`)")
        if cov is None:
            self._fetch_and_store(symbol, start, end)
            return
        cov_start, cov_end = cov
        if start < cov_start:
            self._fetch_and_store(symbol, start, cov_start - timedelta(days=1))
        if end > cov_end:
            overlap_start = cov_end - timedelta(days=OVERLAP_BARS * 2 + 3)
            fresh = self._fetch(symbol, overlap_start, end)
            if self._split_detected(symbol, fresh):
                log.warning("%s: cached adjusted prices disagree with fresh data (split/dividend adjustment "
                            "changed) - refetching full history", symbol)
                self.store.delete_symbol(symbol, adjustment=self.adjustment)
                self._fetch_and_store(symbol, min(start, cov_start), end)
                return
            self._store(symbol, fresh, overlap_start, end)

    def _split_detected(self, symbol: str, fresh: pd.DataFrame) -> bool:
        if fresh.empty:
            return False
        cached = self.store.get_bars(symbol, fresh.index[0].date(), fresh.index[-1].date(), adjustment=self.adjustment)
        common = cached.index.intersection(fresh.index)
        if len(common) == 0:
            return False
        a = cached.loc[common, "close"].to_numpy()
        b = fresh.loc[common, "close"].to_numpy()
        rel = np.abs(a - b) / np.where(b == 0, np.nan, np.abs(b))
        return bool(np.nanmax(rel) > SPLIT_TOLERANCE)

    def _fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        assert self.provider is not None
        log.info("fetching %s daily bars %s..%s from %s", symbol, start, end, self.provider.name)
        return self.provider.fetch_daily(symbol, start, end)

    def _store(self, symbol: str, df: pd.DataFrame, start: date, end: date) -> None:
        assert self.provider is not None
        n = self.store.upsert_bars(symbol, df, adjustment=self.adjustment, source=self.provider.name)
        # Never mark coverage beyond the last bar we actually hold for the *future* edge.
        eff_end = min(end, date.today())
        self.store.set_coverage(symbol, start, eff_end, adjustment=self.adjustment)
        log.info("cached %d bars for %s (%s..%s)", n, symbol, start, eff_end)

    def _fetch_and_store(self, symbol: str, start: date, end: date) -> None:
        df = self._fetch(symbol, start, end)
        if df.empty:
            log.warning("%s: provider returned no bars for %s..%s", symbol, start, end)
        self._store(symbol, df, start, end)
