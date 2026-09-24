import logging
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.config import Settings
from bot.data.calendar import NY, daily_ts, is_opg_window, last_completed_session_date
from bot.data.loader import BarLoader
from bot.data.providers import CsvBarProvider
from bot.data.store import BarStore
from bot.monitoring.logging import RedactFilter
from tests.conftest import make_bars


class ScriptedProvider:
    """Serves slices of a fixed frame and counts calls; optionally applies a 'split' to everything."""
    name = "scripted"

    def __init__(self, df, split=1.0):
        self.df, self.split, self.calls = df, split, []

    def fetch_daily(self, symbol, start, end):
        self.calls.append((start, end))
        out = self.df.loc[daily_ts(start): daily_ts(end)].copy()
        for c in ("open", "high", "low", "close"):
            out[c] = out[c] / self.split
        return out


def test_store_roundtrip_is_ny_tz_and_idempotent():
    df = make_bars(10)
    s = BarStore()
    assert s.upsert_bars("spy", df) == 10
    assert s.upsert_bars("spy", df) == 10
    out = s.get_bars("SPY")
    assert len(out) == 10 and str(out.index.tz) == "America/New_York"
    pd.testing.assert_frame_equal(out[["open", "high", "low", "close", "volume"]], df, check_freq=False, check_names=False)
    assert s.get_bars("SPY", date(2020, 1, 6), date(2020, 1, 7)).shape[0] == 2


def test_loader_uses_cache_and_only_fetches_missing_ranges():
    df = make_bars(300, seed=1)
    prov = ScriptedProvider(df)
    loader = BarLoader(BarStore(), prov)
    a = loader.get_daily("X", date(2020, 3, 1), date(2020, 6, 1), warmup=20)
    assert len(prov.calls) == 1 and len(a) > 60
    b = loader.get_daily("X", date(2020, 3, 1), date(2020, 6, 1), warmup=20)   # fully cached
    assert len(prov.calls) == 1
    pd.testing.assert_frame_equal(a, b)
    loader.get_daily("X", date(2020, 3, 1), date(2020, 9, 1))                   # extends the tail only
    assert len(prov.calls) == 2 and prov.calls[-1][1] == date(2020, 9, 1)
    assert prov.calls[-1][0] > date(2020, 5, 1)


def test_loader_detects_split_and_refetches_history():
    df = make_bars(300, seed=2)
    prov = ScriptedProvider(df)
    loader = BarLoader(BarStore(), prov)
    loader.get_daily("X", date(2020, 3, 1), date(2020, 6, 1))
    prov.split = 2.0   # a 2:1 split: the provider now returns everything halved
    out = loader.get_daily("X", date(2020, 3, 1), date(2020, 9, 1))
    assert prov.calls[-1][0] <= date(2020, 3, 1), "full history must be refetched after the split"
    assert np.allclose(out["close"].to_numpy() * 2, df.loc[out.index, "close"].to_numpy())


def test_csv_provider_ohlcv_and_close_only(tmp_path):
    df = make_bars(5)
    p = tmp_path / "x.csv"
    df.tz_localize(None).rename_axis("Date").to_csv(p)
    out = CsvBarProvider(p).fetch_daily("X", date(2019, 1, 1), date(2021, 1, 1))
    assert len(out) == 5 and str(out.index.tz) == "America/New_York"
    wide = tmp_path / "wide.csv"
    pd.DataFrame({"Date": df.index.strftime("%Y-%m-%d"), "AAA": df["close"].values, "BBB": 1.0}).to_csv(wide, index=False)
    prov = CsvBarProvider(wide, close_column="AAA")
    out = prov.fetch_daily("AAA", date(2019, 1, 1), date(2021, 1, 1))
    assert prov.synthetic_ohlc and (out["open"] == out["close"]).all()


def test_calendar_helpers():
    from datetime import datetime
    assert is_opg_window(datetime(2024, 1, 5, 19, 30, tzinfo=NY))
    assert is_opg_window(datetime(2024, 1, 5, 8, 0, tzinfo=NY))
    assert not is_opg_window(datetime(2024, 1, 5, 12, 0, tzinfo=NY))
    assert last_completed_session_date(datetime(2024, 1, 6, 12, 0, tzinfo=NY)) == date(2024, 1, 5)   # Saturday -> Friday
    assert last_completed_session_date(datetime(2024, 1, 5, 12, 0, tzinfo=NY)) == date(2024, 1, 4)   # mid-session -> Thursday
    assert last_completed_session_date(datetime(2024, 1, 5, 16, 30, tzinfo=NY)) == date(2024, 1, 5)


def test_paper_is_default_and_secrets_are_hidden():
    s = Settings(_env_file=None)
    assert s.trading_env == "paper" and not s.is_live and not s.live_autonomous_trading and s.safe_live_test_mode
    assert not s.has_credentials("paper") and not s.has_credentials("live")
    s = Settings(_env_file=None, alpaca_paper_api_key="PKTESTKEY123", alpaca_paper_secret_key="supersecretvalue")
    assert "PKTESTKEY123" not in repr(s) and "supersecretvalue" not in str(s)
    assert s.credential_status("paper") == {"env": "paper", "key_present": True, "secret_present": True, "key_prefix_ok": True, "key_prefix": "PK"}
    f = RedactFilter(s.secret_values())
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key=%s secret=%s", ("PKTESTKEY123", "supersecretvalue"), None)
    f.filter(rec)
    assert rec.getMessage() == "key=*** secret=***"


def test_repo_has_no_env_file_committed():
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()
    assert ".env" not in tracked and ".env.example" in tracked
