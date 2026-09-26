from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from bot.data.universe import Universe, build_tier3, read_candidates


def test_universe_yaml_loads_and_cap_enforced(tmp_path):
    u = Universe.load("config/universe.yaml")
    assert u.tier1 == ["SPY", "QQQ", "IWM", "DIA"] and len(u.tier2) == 11 and u.tier3 == []
    assert u.sector_of("XLK") == "technology" and u.sector_of("ZZZ") == "unknown"
    u.check_cap("basic")
    u.tier3 = [f"S{i}" for i in range(16)]        # 4 + 11 + 16 = 31 > 30
    with pytest.raises(ValueError, match="Basic data plan"):
        u.check_cap("basic")
    u.check_cap("plus")
    # save/load round trip preserves tiers 1-2 and writes tier 3
    p = tmp_path / "u.yaml"
    p.write_text(open("config/universe.yaml").read())
    u2 = Universe.load(p)
    u2.tier3, u2.tier3_built_on = ["AAPL"], "2026-09-26"
    u2.sectors["AAPL"] = "technology"
    u2.save()
    u3 = Universe.load(p)
    assert u3.tier3 == ["AAPL"] and u3.tier3_built_on == "2026-09-26" and u3.tier1 == u.tier1 and u3.sector_of("AAPL") == "technology"


def test_candidates_file_parses():
    c = read_candidates("config/tier3_candidates.txt")
    assert ("AAPL", "technology") in c and len(c) >= 40


def test_build_tier3_filters_and_ranks():
    cands = [("BIG", "technology"), ("CHEAP", "energy"), ("WIDE", "financials"), ("NOFRAC", "healthcare"), ("MID", "industrials")]
    prices = {"BIG": 200.0, "CHEAP": 10.0, "WIDE": 100.0, "NOFRAC": 300.0, "MID": 50.0}
    volume = {"BIG": 5e6, "CHEAP": 9e6, "WIDE": 3e6, "NOFRAC": 2e6, "MID": 4e6}

    def daily_bars(sym, a, b):
        idx = pd.bdate_range(end=b, periods=70, tz="America/New_York")
        return pd.DataFrame({"close": prices[sym], "volume": volume[sym], "open": prices[sym], "high": prices[sym], "low": prices[sym]}, index=idx)

    def asset_info(sym):
        return SimpleNamespace(fractionable=sym != "NOFRAC", tradable=True, easy_to_borrow=True)

    def spread(sym):
        return [12.0, 11.0] if sym == "WIDE" else [1.0, 1.5, 0.8]
    selected, evaluated = build_tier3(cands, daily_bars=daily_bars, asset_info=asset_info, spread_samples=spread, asof=date(2026, 9, 25), n=2)
    assert [c.symbol for c in selected] == ["BIG", "MID"]
    by = {c.symbol: c for c in evaluated}
    assert "price 10.00 < 20.0" in by["CHEAP"].reasons and any("spread" in r for r in by["WIDE"].reasons) and "not fractionable" in by["NOFRAC"].reasons
    assert by["BIG"].median_dollar_volume == pytest.approx(200.0 * 5e6)
