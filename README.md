# tbot — research-grade trading bot (backtest + Alpaca paper trading)

An event-driven backtester and a paper-trading loop for daily-bar strategies on
US equities, built on the [Alpaca](https://alpaca.markets) paper API.

**This is infrastructure, not alpha.** The two bundled strategies are teaching
baselines. See [REPORT.md](REPORT.md) for what the backtests actually show.

```
bot/
  config.py          every setting from env vars / .env (pydantic-settings, SecretStr)
  data/              Alpaca + CSV providers, DuckDB cache, NY-time helpers, split detection
  strategies/        Strategy base class (on_bar), ma_crossover, mean_reversion, indicators
  backtest/          event-driven engine (no lookahead), cost model, metrics, walk-forward, reports
  risk/              fixed-fractional sizing, daily loss halt, drawdown kill switch, position cap
  execution/         Alpaca broker (retry/backoff), fake broker, persisted state, paper loop
  monitoring/        JSON-lines logging with secret redaction, CLI dashboard, Discord alerts
  cli.py             python -m bot ...
tests/               lookahead, risk, costs, restart safety, data cache
scripts/load_sample_data.py   real historical bars without an Alpaca account (see below)
```

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # or requirements.txt without pytest
cp .env.example .env                       # then paste your paper keys into .env
python -m pytest                           # 38 tests, ~10 s, fully offline
```

### Free Alpaca paper keys

1. Sign up at <https://app.alpaca.markets> (no funding needed).
2. Switch the account toggle (top-left) to **Paper Trading**.
3. Open *API Keys* on the right, click *Generate*, copy the key ID and secret.
4. Put them in `.env` as `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.

Paper keys only work against `https://paper-api.alpaca.markets`, which is what
the bot uses unless you go through the live-trading gates described below.
`.env` is git-ignored; keys are `SecretStr` and a log filter redacts them.

## Run a backtest

```bash
python -m bot backtest --strategy ma_crossover --symbol SPY --start 2023-01-01 --end 2025-12-31
python -m bot backtest --strategy mean_reversion --symbol SPY --symbol QQQ --start 2016-01-01 --end 2025-12-31 \
    --param lookback=20 --param entry_z=2.0
```

What you get:

- a full-period run with the given (or default) parameters, next to equal-weight buy-and-hold;
- a **walk-forward** run: rolling 3-year train / 1-year test windows, the parameter
  grid is searched on train only, the winner is run once on the following test year,
  and the test years are stitched into one out-of-sample curve;
- Sharpe, Sortino, CAGR, max drawdown, Calmar, win rate, profit factor, trade
  count, time in market, costs paid, risk events; and a one-line verdict that says
  "LOSES MONEY after costs" when it does;
- CSV/JSON outputs in `reports/`.

Bars are fetched from Alpaca on first use and cached in `data_cache/bars.duckdb`.
Only missing date ranges are fetched afterwards; a small overlap is re-fetched
each time and, if the cached adjusted prices disagree (a split happened), the
symbol's history is discarded and refetched.

Costs and risk limits come from `.env`: slippage + half-spread per side (default
3 bps per side), fixed-fractional sizing (1% of equity at risk per trade, stop =
2×ATR(14)), max 50% of equity per position, no leverage, daily loss halt at −3%,
kill switch at −20% from peak equity, max 5 concurrent positions. The backtester
and the paper loop share the same `RiskManager`.

### Backtesting without Alpaca keys

`python -m bot data import --symbol SPY --csv spy.csv` loads any daily CSV
(date + open/high/low/close/volume, or close-only with `--close-column`).

`python scripts/load_sample_data.py` loads real history that ships inside two
PyPI wheels (downloaded, not installed): Google daily OHLCV 2004–2013, the S&P
500 index and 20 large-cap stocks' daily closes 1990–2022, five factor ETFs
2014–2022. Symbols: `GOOG`, `SP500`, `AAPL`, `MSFT`, … The close-only series get
open = high = low = close, so fills happen at the next close instead of the next
open. This is how the numbers in REPORT.md were produced.

## Paper trade

```bash
python -m bot paper --strategy ma_crossover --symbol SPY --symbol QQQ          # long-running loop
python -m bot paper --strategy ma_crossover --symbol SPY --once                # one cycle, cron-friendly
python -m bot dashboard --watch 30                                             # equity, positions, trades, orders
python -m bot status                                                           # account, positions, clock
python -m bot risk show / python -m bot risk reset                             # kill switch state
```

How a cycle works (daily bars):

1. Sync order status with the broker, book fills, record closed trades.
2. Read account equity, feed the risk manager. A kill switch trip liquidates
   everything, sends a critical alert, and refuses to trade until `risk reset`.
3. Find the last completed session (Alpaca calendar). For each symbol not yet
   processed for that session: load bars, replay the strategy over its warm-up
   window, compute the desired exposure, apply the protective stop, compare with
   the broker's actual position, and submit at most one order.
4. Orders are market-on-open (`ORDER_TIME_IN_FORCE=opg`), submitted in Alpaca's
   OPG window (19:00–09:28 ET) so they fill in the opening auction, matching the
   backtester's "fill at next open" assumption. Between 16:00 and 19:00 the loop
   waits; if the market is open it falls back to a `day` market order.

Restart safety: state (`state/paper.json`) is written atomically before *and*
after every submission; each order has a deterministic `client_order_id`
(`paper-SPY-2025-01-15-entry`), which Alpaca refuses to accept twice. A crash at
any point is recovered by looking the order up by that id. Positions closed
outside the bot are detected and dropped from state; positions the bot did not
open are adopted without a stop and logged.

Monitoring: `logs/bot.jsonl` (structured events: orders, fills, risk events,
errors) plus a readable console stream. Set `DISCORD_WEBHOOK_URL` to get alerts
for orders, fills, daily halts, kill switch, and cycle errors.

## Live trading is disabled

`LIVE_TRADING=false` is the default. Turning it on requires all three:

1. `LIVE_TRADING=true` in the environment,
2. `--i-understand-live-trading` on the command line,
3. typing `LIVE` at an interactive confirmation prompt (non-interactive runs abort).

Nothing in this repo has been run against a live account.

## Writing a strategy

```python
from bot.strategies.base import Bar, Signal, Strategy

class MyStrategy(Strategy):
    name = "my_strategy"
    default_params = {"n": 20}

    @property
    def warmup(self) -> int:               # bars needed before the first signal
        return self.params["n"]

    def reset(self) -> None:               # clear state; called before every run
        self.closes = []

    def on_bar(self, bar: Bar) -> Signal | None:
        self.closes.append(bar.close)      # you only ever see completed bars, in order
        if len(self.closes) < self.params["n"]:
            return None
        return Signal(bar.symbol, target=1 if bar.close > min(self.closes[-self.params["n"]:]) else 0)

    @classmethod
    def param_grid(cls):                   # searched by walk-forward, on train data only
        return [{"n": n} for n in (10, 20, 50)]
```

Register it in `bot/strategies/__init__.py`. Signals are desired exposure
(+1/0/−1) and are filled at the *next* bar's open; the engine sizes them, attaches
an ATR stop, and enforces the risk limits.

## Limitations

- Daily bars only. Intraday is intentionally not built yet.
- Long/flat by default; `mean_reversion` can short with `allow_short=true`, and
  the engine/loop handle short positions, but shorting was not evaluated.
- Walk-forward folds reset positions and risk state at each boundary.
- No dividends in `DATA_ADJUSTMENT=split` mode (use `all` for total-return prices).
- The offline calendar fallback ignores exchange holidays; with Alpaca configured
  the broker calendar is used.
