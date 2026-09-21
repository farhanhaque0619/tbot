"""Local bar cache in DuckDB.

Timestamps are stored as naive UTC ``TIMESTAMP`` values and converted to
America/New_York on the way out, so the on-disk format is unambiguous and
callers only ever see NY time.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from bot.data.calendar import to_ny_index

log = logging.getLogger(__name__)

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]
OPTIONAL_COLUMNS = ["trade_count", "vwap"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      VARCHAR NOT NULL,
    timeframe   VARCHAR NOT NULL,
    adjustment  VARCHAR NOT NULL,
    ts          TIMESTAMP NOT NULL,   -- UTC, naive
    open        DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume      DOUBLE,
    trade_count DOUBLE, vwap DOUBLE,
    source      VARCHAR,
    fetched_at  TIMESTAMP,
    PRIMARY KEY (symbol, timeframe, adjustment, ts)
);
CREATE TABLE IF NOT EXISTS coverage (
    symbol VARCHAR NOT NULL, timeframe VARCHAR NOT NULL, adjustment VARCHAR NOT NULL,
    start_date DATE, end_date DATE, updated_at TIMESTAMP,
    PRIMARY KEY (symbol, timeframe, adjustment)
);
CREATE TABLE IF NOT EXISTS equity_history (
    run_id VARCHAR NOT NULL, ts TIMESTAMP NOT NULL, equity DOUBLE, cash DOUBLE,
    PRIMARY KEY (run_id, ts)
);
"""


class BarStore:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(self.path)
        self.con.execute("SET TimeZone='UTC'")
        for stmt in _SCHEMA.strip().split(";"):
            if stmt.strip():
                self.con.execute(stmt)

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------------ write
    def upsert_bars(self, symbol: str, df: pd.DataFrame, *, timeframe: str = "1Day",
                    adjustment: str = "split", source: str = "alpaca") -> int:
        """Insert-or-replace bars. ``df`` must be indexed by tz-aware timestamps."""
        if df.empty:
            return 0
        out = df.copy()
        idx = pd.DatetimeIndex(out.index)
        if idx.tz is None:
            raise ValueError("bar index must be tz-aware")
        out.index = idx.tz_convert("UTC").tz_localize(None)
        for c in OPTIONAL_COLUMNS:
            if c not in out.columns:
                out[c] = float("nan")
        out = out[BAR_COLUMNS + OPTIONAL_COLUMNS].astype(float)
        out = out.reset_index().rename(columns={out.index.name or "index": "ts"})
        out.insert(0, "symbol", symbol.upper())
        out.insert(1, "timeframe", timeframe)
        out.insert(2, "adjustment", adjustment)
        out["source"] = source
        out["fetched_at"] = pd.Timestamp.utcnow().tz_localize(None)
        self.con.register("_new_bars", out)
        self.con.execute("INSERT OR REPLACE INTO bars SELECT * FROM _new_bars")
        self.con.unregister("_new_bars")
        return len(out)

    def set_coverage(self, symbol: str, start: date, end: date, *, timeframe: str = "1Day",
                     adjustment: str = "split") -> None:
        cur = self.get_coverage(symbol, timeframe=timeframe, adjustment=adjustment)
        if cur:
            start, end = min(start, cur[0]), max(end, cur[1])
        self.con.execute(
            "INSERT OR REPLACE INTO coverage VALUES (?, ?, ?, ?, ?, now())",
            [symbol.upper(), timeframe, adjustment, start, end],
        )

    def delete_symbol(self, symbol: str, *, timeframe: str = "1Day", adjustment: str | None = None) -> None:
        params = [symbol.upper(), timeframe]
        clause = "symbol = ? AND timeframe = ?"
        if adjustment:
            clause += " AND adjustment = ?"
            params.append(adjustment)
        self.con.execute(f"DELETE FROM bars WHERE {clause}", params)
        self.con.execute(f"DELETE FROM coverage WHERE {clause}", params)

    # ------------------------------------------------------------------- read
    def get_coverage(self, symbol: str, *, timeframe: str = "1Day",
                     adjustment: str = "split") -> tuple[date, date] | None:
        row = self.con.execute(
            "SELECT start_date, end_date FROM coverage WHERE symbol=? AND timeframe=? AND adjustment=?",
            [symbol.upper(), timeframe, adjustment],
        ).fetchone()
        return (row[0], row[1]) if row else None

    def get_bars(self, symbol: str, start: date | None = None, end: date | None = None, *,
                 timeframe: str = "1Day", adjustment: str = "split") -> pd.DataFrame:
        q = "SELECT ts, open, high, low, close, volume, trade_count, vwap FROM bars WHERE symbol=? AND timeframe=? AND adjustment=?"
        params: list = [symbol.upper(), timeframe, adjustment]
        if start is not None:
            q += " AND ts >= ?"
            params.append(pd.Timestamp(start, tz="America/New_York").tz_convert("UTC").tz_localize(None))
        if end is not None:
            q += " AND ts < ?"
            params.append((pd.Timestamp(end, tz="America/New_York") + pd.Timedelta(days=1)).tz_convert("UTC").tz_localize(None))
        q += " ORDER BY ts"
        df = self.con.execute(q, params).df()
        if df.empty:
            return pd.DataFrame(columns=BAR_COLUMNS + OPTIONAL_COLUMNS, index=pd.DatetimeIndex([], tz="America/New_York", name="ts"))
        df["ts"] = to_ny_index(pd.DatetimeIndex(df["ts"]))
        return df.set_index("ts")

    def symbols(self, *, timeframe: str = "1Day") -> list[str]:
        rows = self.con.execute("SELECT DISTINCT symbol FROM bars WHERE timeframe=? ORDER BY symbol", [timeframe]).fetchall()
        return [r[0] for r in rows]

    def summary(self) -> pd.DataFrame:
        return self.con.execute(
            "SELECT symbol, timeframe, adjustment, count(*) AS bars, min(ts) AS first_ts, max(ts) AS last_ts, "
            "any_value(source) AS source FROM bars GROUP BY 1,2,3 ORDER BY 1,2,3"
        ).df()

    # ---------------------------------------------------------- equity curve
    def append_equity(self, run_id: str, ts, equity: float, cash: float) -> None:
        t = pd.Timestamp(ts)
        t = (t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")).tz_localize(None)
        self.con.execute("INSERT OR REPLACE INTO equity_history VALUES (?, ?, ?, ?)", [run_id, t, float(equity), float(cash)])

    def equity_history(self, run_id: str) -> pd.DataFrame:
        df = self.con.execute("SELECT ts, equity, cash FROM equity_history WHERE run_id=? ORDER BY ts", [run_id]).df()
        if df.empty:
            return df
        df["ts"] = to_ny_index(pd.DatetimeIndex(df["ts"]))
        return df.set_index("ts")
