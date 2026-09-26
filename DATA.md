# DATA — sources, adjustments, and what the numbers mean

## Sources

| Source | Used for | Type | Notes |
|---|---|---|---|
| Alpaca Market Data v2 (`AlpacaBarProvider`) | backtests and the live/paper loop when credentials exist | daily OHLCV, `trade_count`, `vwap` | default and only production source |
| CSV import (`python -m bot data import`) | research without credentials | OHLCV or close-only | close-only files get open=high=low=close |
| Bundled wheel datasets (`scripts/load_sample_data.py`) | REPORT.md numbers produced in a network-blocked environment | GOOG OHLCV 2004–2013; S&P 500 index + 20 stocks + 5 ETFs, close-only, 1990–2022 | research proxies, not tradable symbols |

**Status on 2026-09-24:** the build environment cannot reach `data.alpaca.markets`. No Alpaca bars have been
fetched by this code yet. The first operator action is `python -m bot data fetch --symbol SPY --symbol QQQ
--start 2015-01-01 --end <today>` on a machine with credentials, followed by `python -m bot data check --symbol SPY --symbol QQQ`.

## Explicit answers

- **Raw vs adjusted.** `DATA_ADJUSTMENT=split` (default): prices are adjusted for splits only, so historical prices
  are comparable and the *most recent* prices equal what trades at. `all` adds dividend adjustment (a total-return
  series; historical prices then differ from what traded). `raw` is unadjusted. The adjustment is part of the cache
  key; changing it refetches.
- **Splits.** Requested split-adjusted from Alpaca. Because a new split rewrites the whole adjusted history, the
  loader re-fetches a 5-bar overlap on every cache extension; >0.5% disagreement discards and refetches the symbol.
  The trading loop trusts the broker's share count after a split (reconciliation), but a position's *stop price* is
  not adjusted: an open position across a split will have a stale stop until the next entry. Known limitation.
- **Dividends.** Not in prices under `split`. Backtest returns therefore exclude dividends for both strategy and
  buy-and-hold (fair comparison, understated absolute returns). Cash dividends received in the paper/live account
  show up in equity but are not attributed to trades.
- **Symbol changes / delistings.** Not handled. A renamed ticker is a new symbol with no history; a delisted symbol
  stops producing bars and the loop logs "bar not available yet" forever. Use `data check` to notice.
- **Missing bars.** Backtester iterates over the union of available dates; a symbol without a bar on a date is
  simply not updated that day. The loop waits (does not mark the session processed) until the bar for the last
  completed session appears. `data check` lists missing weekdays (holidays included, since no holiday table is
  bundled; the broker calendar is authoritative when online).
- **Time zones.** Stored as naive UTC in DuckDB, exposed as `America/New_York` everywhere. Daily bars are stamped at
  NY midnight of the session date. Alpaca returns daily bars at 04:00/05:00Z which is the same NY date.
- **Market holidays.** Live: Alpaca `/calendar`. Offline fallback: weekdays only, which mislabels holidays as
  sessions (the loop then waits for a bar that never comes, harmlessly, until the next real session).
- **Pre/post-market.** Daily bars from Alpaca cover the regular session only for the OHLC; `volume` is the
  consolidated day. Orders are never flagged `extended_hours`. OPG orders fill only in the opening auction.
- **Feed.** `DATA_FEED=sip` (consolidated). The free plan cannot query SIP data younger than 15 minutes, so the
  provider never asks for anything past *now − 16 min* and falls back to `iex` on a subscription error. Quotes used
  for spread/price sanity in the loop are subject to the same rule: on the free plan they may be IEX-only.
- **Lookahead.** Strategies receive completed bars one at a time (`on_bar`) and hold no handle to future data;
  fills happen at the next bar's open; `MarketState` is computed from bars up to the decision bar. Regression tests
  in `tests/test_no_lookahead.py` mutate every future bar and assert that earlier equity, trades, loop decisions,
  and market-state values are unchanged.

## Why a request from 2015-01-01 returns history starting 2016-01-04

Alpaca's historical stock data (bars, trades, quotes) starts on **2016-01-01**; 2016-01-04 is the first trading
day of that year. The SDK cannot have truncated the request: `get_stock_bars` paginates automatically at 10,000
bars per page and a 2015→2026 daily series is ~3,000 bars, a single page. Earlier history has to come from
another source (`python -m bot data import` with a CSV) or the backtest start must be ≥ 2016 plus warm-up.

The loader records coverage from the *requested* start so the empty range is not re-requested on every run, and
logs a warning whenever the first returned bar is more than 10 days after the requested start (history floor,
listing date, or a gap). `python -m bot data check` shows the actual first bar.

## Quality checks

`python -m bot data check --symbol SPY` reports coverage, source, missing weekdays, duplicates, non-positive prices,
OHLC consistency, one-day moves above 40% (possible unadjusted split or bad print), zero-volume share, and whether
the data is close-only.
