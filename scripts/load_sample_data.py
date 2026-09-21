"""Load real historical daily bars into the local cache WITHOUT an Alpaca account.

Two PyPI packages ship real price history inside their wheels; we download the
wheels (no install, no dependencies) and extract the CSVs:

- ``backtesting`` (backtesting.py, AGPL-3):  GOOG daily OHLCV 2004-08 .. 2013-03
- ``skfolio`` (BSD-3):  S&P 500 index daily closes 1990 .. 2022, twenty S&P 500
  stocks' daily (adjusted) closes 1990 .. 2022, five factor ETFs 2014 .. 2022.

The skfolio files are close-only, so open/high/low are set equal to close and
volume to 0 (the backtester then fills "at the next open" = at the next close).
The data is used purely as research input; nothing from those packages is
imported or redistributed by this repo.

Usage:  python scripts/load_sample_data.py [--symbols SP500,GOOG,AAPL,...]
"""
from __future__ import annotations

import argparse
import gzip
import io
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import get_settings  # noqa: E402
from bot.data.calendar import daily_ts  # noqa: E402
from bot.data.store import BarStore  # noqa: E402

WHEELS = {"backtesting": "backtesting==0.6.6", "skfolio": "skfolio==1.3.0"}


def download_wheels(dest: Path) -> dict[str, Path]:
    subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "-q", "-d", str(dest), *WHEELS.values()], check=True)
    out = {}
    for name in WHEELS:
        out[name] = next(dest.glob(f"{name}-*.whl"))
    return out


def read_from_wheel(whl: Path, member: str) -> bytes:
    with zipfile.ZipFile(whl) as z:
        return z.read(member)


def frame_ohlcv(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), index_col=0, parse_dates=True)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df.index = pd.DatetimeIndex([daily_ts(t.date()) for t in df.index], name="ts")
    return df


def frames_close_only(raw_gz: bytes) -> dict[str, pd.DataFrame]:
    wide = pd.read_csv(io.BytesIO(gzip.decompress(raw_gz)), index_col=0, parse_dates=True)
    out = {}
    for col in wide.columns:
        c = pd.to_numeric(wide[col], errors="coerce").dropna()
        df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 0.0})
        df.index = pd.DatetimeIndex([daily_ts(t.date()) for t in df.index], name="ts")
        out[col.upper()] = df.astype(float)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="comma-separated subset (default: everything)")
    args = ap.parse_args()
    wanted = {s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None
    settings = get_settings()
    store = BarStore(settings.data_db_path)
    with tempfile.TemporaryDirectory() as tmp:
        wheels = download_wheels(Path(tmp))
        datasets: dict[str, tuple[pd.DataFrame, str]] = {}
        datasets["GOOG"] = (frame_ohlcv(read_from_wheel(wheels["backtesting"], "backtesting/test/GOOG.csv")), "backtesting.py sample (OHLCV)")
        for member, tag in (("skfolio/datasets/data/sp500_index.csv.gz", "skfolio sp500_index (close-only)"),
                            ("skfolio/datasets/data/sp500_dataset.csv.gz", "skfolio sp500_dataset (close-only)"),
                            ("skfolio/datasets/data/factors_dataset.csv.gz", "skfolio factors_dataset (close-only)")):
            for sym, df in frames_close_only(read_from_wheel(wheels["skfolio"], member)).items():
                datasets[sym] = (df, tag)
    for sym, (df, tag) in datasets.items():
        if wanted and sym not in wanted:
            continue
        store.delete_symbol(sym, adjustment=settings.data_adjustment)
        n = store.upsert_bars(sym, df, adjustment=settings.data_adjustment, source=f"sample:{tag}")
        store.set_coverage(sym, df.index[0].date(), df.index[-1].date(), adjustment=settings.data_adjustment)
        print(f"{sym:6s} {n:5d} bars {df.index[0].date()} → {df.index[-1].date()}   [{tag}]")
    store.close()


if __name__ == "__main__":
    main()
