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

## Minute bars (V1.5 Phase 1)

- **Tables.** `bars_1m(symbol, ts, o, h, l, c, v, trade_count, vwap, feed, backfilled, fetched_at)` keyed by
  (symbol, feed, ts) with `ts` = bar START in naive UTC; `quotes(symbol, ts, bid, ask, bid_size, ask_size, feed)`;
  `minute_coverage(symbol, feed, session_date, bars, complete)`; `sessions(session_date, open_ts, close_ts, source)`.
  `bars_1d` is a view over the existing daily `bars` table.
- **Feeds and lag.** `DATA_FEED=sip` is the consolidated tape. On the Basic plan SIP data younger than 15 minutes is
  refused, so `fetch_minute` never requests past now − 16 min for SIP (`sip_lagged_end`), and IEX is requested with
  no lag. A subscription refusal is raised as `DataPlanError`; there is deliberately no silent fallback to IEX for
  a query that asked for SIP, because IEX volume is a small fraction of the tape and IEX prices can differ by a tick.
- **Fetch.** `python -m bot data fetch --timeframe 1m --symbol SPY --start 2016-01-04 --end 2026-09-26 --feed sip`
  fetches session by session in chunks and is resumable: a session is marked complete in `minute_coverage` when it
  holds ≥ 98% of the expected minutes and its close is at least 16 minutes old (SIP); incomplete sessions are
  re-requested on the next run. Roughly 2.6 million bars per symbol for 2016–2026; ~260 requests of 10,000 bars.
- **Sessions.** `bot/data/sessions.py` combines the broker calendar (synced into the `sessions` table, authoritative)
  with exact NYSE rules as the offline fallback: holidays with Saturday→Friday / Sunday→Monday observance (except
  New Year's Day on a Saturday), Good Friday via the Gregorian Easter algorithm, Juneteenth from 2022, 13:00 early
  closes (day after Thanksgiving; July 3 and December 24 when they fall Monday–Thursday), and the known special
  closures (2018-12-05, 2025-01-09). Cutoffs: OPG 09:28, CLS 15:50 (12:50 on an early close).
- **Aggregation.** `aggregate(bars_1m, minutes, calendar)` buckets on exact ET boundaries from each session's open,
  drops bars outside regular hours and on non-session dates, and flags the last bar of a session `is_session_end` and
  `partial` when the session closes before the bar's natural end. Timestamps stay correct across DST changes because
  bucketing happens in New York time.
- **Quality.** `python -m bot data check --timeframe 1m --symbol SPY` reports missing minutes per session (2%
  tolerance), duplicates, OHLC consistency, bars outside the session, zero-volume runs ≥ 5 minutes, session-to-session
  jumps > 20% (unadjusted splits), and the share of backfilled bars.
- **Universe.** `config/universe.yaml`: Tier 1 SPY QQQ IWM DIA, Tier 2 the eleven sector ETFs, Tier 3 written by
  `python -m bot universe build` from `config/tier3_candidates.txt` (a static large-cap candidate list, see D4 in
  V1_5_AUDIT.md) using 60-session median dollar volume, price ≥ $20, fractionable/tradable/easy-to-borrow flags and a
  sampled quoted spread ≤ 5 bps. `universe show` enforces the 30-symbol Basic-plan cap (`DATA_PLAN=plus` lifts it).
