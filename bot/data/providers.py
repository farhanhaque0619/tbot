"""Bar providers: Alpaca (default) and CSV (offline / research)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd

from bot.config import Settings
from bot.config import NY_TZ
from bot.data.calendar import NY, UTC, daily_ts, to_ny_index
from bot.data.store import BAR_COLUMNS
from bot.utils.retry import with_retry

log = logging.getLogger(__name__)


class DataPlanError(RuntimeError):
    """The data plan does not permit this query (e.g. SIP data younger than 15 minutes on the Basic plan).
    Raised instead of silently falling back so that a query that needs recency never gets stale data."""


SIP_LAG = timedelta(minutes=16)


def sip_lagged_end(end: datetime, now: datetime, *, feed: str) -> datetime:
    """Basic plan: SIP data newer than 15 minutes is refused; we never ask past now - 16 min. IEX has no lag."""
    return min(end, now - SIP_LAG) if feed == "sip" else end


def is_subscription_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "subscription" in text or getattr(exc, "status_code", None) == 403 and "permit" in text


class BarProvider(Protocol):
    name: str

    def fetch_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Return daily bars indexed by NY-tz timestamps with columns open/high/low/close/volume(+optional)."""
        ...


class AlpacaBarProvider:
    """Historical daily bars from Alpaca Market Data v2 via alpaca-py."""

    name = "alpaca"

    def __init__(self, settings: Settings, *, env: str = "paper"):
        from alpaca.data.historical import StockHistoricalDataClient

        key, secret = settings.credentials(env)   # market data works with either pair; paper keys are preferred
        self.settings = settings
        self.env = env
        self.client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        del key, secret

    def fetch_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        from alpaca.common.exceptions import APIError
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        # Free plan: SIP data must be >15 minutes old. Daily bars for today are not final until the
        # close anyway, so we never ask for anything past "now - 16 min".
        end_dt = min(datetime.combine(end, datetime.max.time(), NY),
                     datetime.now(NY) - timedelta(minutes=16))
        start_dt = datetime.combine(start, datetime.min.time(), NY)
        feed = DataFeed(self.settings.data_feed)

        def _call(feed_: DataFeed):
            req = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                start=start_dt.astimezone(UTC), end=end_dt.astimezone(UTC),
                adjustment=Adjustment(self.settings.data_adjustment), feed=feed_,
            )
            return self.client.get_stock_bars(req)

        try:
            bars = with_retry(lambda: _call(feed), what=f"get_stock_bars({symbol})")
        except APIError as e:
            # Basic plan can't use SIP for some queries -> transparently fall back to IEX.
            if feed == DataFeed.SIP and "subscription" in str(e).lower():
                log.warning("SIP feed not available for %s on this plan; falling back to IEX", symbol)
                bars = with_retry(lambda: _call(DataFeed.IEX), what=f"get_stock_bars({symbol},iex)")
            else:
                raise
        df = bars.df
        if df.empty:
            return _empty()
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = to_ny_index(df.index)
        # Daily bars are stamped at 00:00 ET (04:00/05:00Z). Normalize to NY midnight for safety.
        df.index = pd.DatetimeIndex([daily_ts(t.date()) for t in df.index], name="ts")
        cols = [c for c in BAR_COLUMNS + ["trade_count", "vwap"] if c in df.columns]
        return df[cols].astype(float).sort_index()


    def fetch_minute(self, symbol: str, start: datetime, end: datetime, *, feed: str | None = None,
                     now: datetime | None = None) -> pd.DataFrame:
        """1-minute bars in [start, end) as tz-aware NY bar-start timestamps. SIP is lagged 16 minutes on the
        Basic plan; a subscription refusal is raised as DataPlanError (no silent fallback)."""
        from alpaca.common.exceptions import APIError
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        feed = feed or self.settings.data_feed
        now = now or datetime.now(NY)
        start = start if start.tzinfo else start.replace(tzinfo=NY)
        end = end if end.tzinfo else end.replace(tzinfo=NY)
        end = sip_lagged_end(end, now.astimezone(NY), feed=feed)
        if end <= start:
            return _empty()
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start.astimezone(UTC), end=end.astimezone(UTC),
                               adjustment=Adjustment(self.settings.data_adjustment), feed=DataFeed(feed))
        try:
            bars = with_retry(lambda: self.client.get_stock_bars(req), what=f"get_stock_bars_1m({symbol},{feed})")
        except APIError as e:
            if is_subscription_error(e):
                raise DataPlanError(f"{feed} minute bars refused for {symbol} {start}..{end}: {e}") from e
            raise
        df = bars.df
        if df.empty:
            return _empty()
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = to_ny_index(df.index)
        df.index.name = "ts"
        cols = [c for c in BAR_COLUMNS + ["trade_count", "vwap"] if c in df.columns]
        return df[cols].astype(float).sort_index()


class CsvBarProvider:
    """Loads daily bars from a CSV file (date + OHLCV, or close-only).

    Accepts a ``Date``/``date``/``timestamp`` column (or unnamed first column) and
    case-insensitive ``open/high/low/close/volume`` columns. Close-only files get
    open=high=low=close and volume=0 and are tagged so reports can say so.
    """

    name = "csv"

    def __init__(self, path: Path | str, *, close_column: str | None = None):
        self.path = Path(path)
        self.close_column = close_column
        self.synthetic_ohlc = False

    def fetch_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        raw = pd.read_csv(self.path)
        cols = {c.lower(): c for c in raw.columns}
        date_col = next((cols[c] for c in ("date", "timestamp", "ts", "time") if c in cols), raw.columns[0])
        raw[date_col] = pd.to_datetime(raw[date_col], utc=False)
        raw = raw.set_index(date_col).sort_index()
        if self.close_column:
            close = pd.to_numeric(raw[self.close_column], errors="coerce")
            df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 0.0})
            self.synthetic_ohlc = True
        elif all(c in cols for c in ("open", "high", "low", "close")):
            df = pd.DataFrame({c: pd.to_numeric(raw[cols[c]], errors="coerce") for c in ("open", "high", "low", "close")})
            df["volume"] = pd.to_numeric(raw[cols["volume"]], errors="coerce") if "volume" in cols else 0.0
        elif "close" in cols:
            close = pd.to_numeric(raw[cols["close"]], errors="coerce")
            df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 0.0})
            self.synthetic_ohlc = True
        else:
            raise ValueError(f"{self.path}: need open/high/low/close columns or --close-column")
        df = df.dropna(subset=["close"])
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_convert(NY_TZ)
        df.index = pd.DatetimeIndex([daily_ts(t.date()) for t in idx], name="ts")
        df = df[~df.index.duplicated(keep="last")]
        return df.loc[daily_ts(start): daily_ts(end)].astype(float)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_COLUMNS, index=pd.DatetimeIndex([], tz=NY_TZ, name="ts"))
