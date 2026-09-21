# REPORT — what was built, what the backtests show, and whether anything deserves paper trading

Date: 2026-09-21. All numbers below are reproducible with the commands listed in each section.

## 1. What was built

| Module | What it does | Verified by |
|---|---|---|
| `bot/config.py` | Every setting from env vars / `.env`; keys are `SecretStr`; `LIVE_TRADING=false` default | `test_live_trading_is_off_by_default_and_secrets_are_hidden`, `test_repo_has_no_env_file_committed` |
| `bot/data/` | Alpaca daily bars (split-adjusted, SIP→IEX fallback, free-plan 15-min rule), CSV import, DuckDB cache that fetches only missing ranges, split detection via overlap re-fetch, all timestamps America/New_York | `test_loader_uses_cache_and_only_fetches_missing_ranges`, `test_loader_detects_split_and_refetches_history`, `test_store_roundtrip_is_ny_tz_and_idempotent`, `test_csv_provider_*` |
| `bot/strategies/` | `Strategy.on_bar(bar) -> Signal` interface; `ma_crossover`, `mean_reversion`; incremental SMA/std/ATR | `test_ma_crossover_*`, `test_mean_reversion_*`, `test_rolling_indicators_match_pandas` |
| `bot/backtest/` | Event-driven engine: signal at close of *t* → fill at open of *t+1* with slippage + half-spread; ATR stops checked on completed bars; walk-forward with grid search on train windows only; Sharpe/Sortino/CAGR/MaxDD/Calmar/win rate/profit factor/trade count/exposure | `test_future_bars_do_not_change_past_decisions`, `test_signal_fills_at_next_bars_open_not_this_close`, `test_equity_on_signal_bar_is_unchanged_by_the_signal`, `test_walk_forward_selects_params_on_train_only`, `test_costs_are_applied_per_side_and_pnl_is_exact` |
| `bot/risk/` | Fixed-fractional sizing (1% of equity per trade against a 2×ATR stop), no leverage, daily loss halt, drawdown kill switch, max positions | `test_kill_switch_trips_on_drawdown_and_stays_tripped`, `test_daily_loss_limit_halts_then_resets_next_day`, `test_backtest_kill_switch_liquidates_and_stops_trading`, `test_backtest_protective_stop_exits_position` |
| `bot/execution/` | Alpaca broker with retry/backoff on 429/5xx/network; paper loop with atomic state file and deterministic client order ids; market-on-open orders | `test_one_order_per_session_even_across_restarts`, `test_crash_after_submit_before_state_save_does_not_double_order`, `test_kill_switch_liquidates_and_blocks_trading`, `test_fill_then_stop_then_trade_recorded` |
| `bot/monitoring/` | JSON-lines log with secret redaction, `rich` CLI dashboard (equity sparkline, positions, trades, orders), optional Discord webhook alerts | dashboard exercised manually; alerter used in the paper-loop tests |

38 tests, all passing, all offline (`python -m pytest`).

## 2. Data used, and an important caveat

The build environment had no outbound access to Alpaca (or any other market-data
host), so no SPY/QQQ bars from Alpaca were available. Everything below runs on
real historical prices that ship inside two PyPI wheels
(`scripts/load_sample_data.py`):

| Symbol(s) | Source | Period | Type |
|---|---|---|---|
| `SP500` | S&P 500 index (skfolio dataset) | 1990–2022 | daily **close only** |
| `AAPL MSFT JPM XOM KO` | skfolio dataset (adjusted closes) | 1990–2022 | daily **close only** |
| `GOOG` | backtesting.py sample | 2004-08 – 2013-03 | daily **OHLCV** |

Close-only series get open = high = low = close. Consequences: (a) "fill at the next
open" becomes "fill at the next close", i.e. a full extra day of delay between
signal and fill — this is *more* pessimistic than reality for the decision lag but
ignores intraday gaps; (b) ATR is computed from close-to-close moves only; (c) the
protective stop can only trigger on a close, which is also how the live loop works.
`SP500` is an index, not an ETF: no expense ratio, no dividends, no tracking error.
The five-stock basket is **survivorship-biased** by construction (they are in the
dataset because they are famous today) — treat that basket's buy-and-hold return as
a warning, not a benchmark.

Costs: 2 bps slippage + 1 bp half-spread per side (6 bps round trip), no commission.
For SPY/QQQ at Alpaca this is realistic; for single stocks it is on the low side.

Risk settings (defaults): 1% of equity risked per trade against a 2×ATR(14) stop,
max 50% of equity per position, no leverage, daily loss halt at −3%, kill switch at
−20% from peak, max 5 positions. Because of the sizing, the strategies are typically
**30–65% invested**, while buy-and-hold is 100% invested. Compare Sharpe and max
drawdown, not raw return; an "aggressive" variant (2% risk, 100% cap) is included
to show the return side.

Walk-forward: 3-year train / 1-year test, rolling, 20 folds on 2000–2022 (5 on
GOOG). The parameter grid is searched on the train window only, the winner by
train Sharpe (minimum 5 trades) is run once on the test year, test years are
stitched by compounding daily returns. Positions and risk state reset at fold
boundaries; positions open at the end of a run are closed at the last bar so trade
statistics and equity agree.

## 3. Results

### 3.1 `ma_crossover` on SP500, 2000-01-03 → 2022-12-28

`python -m bot backtest --strategy ma_crossover --symbol SP500 --start 2000-01-03 --end 2022-12-28`

| | Full period, fixed 50/200 | Walk-forward OOS (2003–2022) | Buy & hold (same OOS window) |
|---|---|---|---|
| Total return | +93.9% | +56.7% | +318.5% |
| CAGR | +2.92% | +2.27% | +7.43% |
| Sharpe | 0.51 | **0.60** | 0.47 |
| Ann. volatility | 6.1% | 3.9% | 19.2% |
| Max drawdown | −14.5% | **−8.8%** | −56.6% |
| Trades | 26 | 53 | – |
| Win rate | 50% | 43% | – |
| Profit factor | 6.27 | 2.56 | – |
| Time in market | 65% | 34% | – |
| Costs paid | $956 | – | – |

Fold detail (params chosen on train only):

| Test year | Params | Test return | Test Sharpe | Trades | B&H |
|---|---|---|---|---|---|
| 2003 | 50/100 | +8.6% | 1.53 | 1 | +22.0% |
| 2004 | 50/100 | +2.8% | 1.17 | 2 | +8.0% |
| 2005 | 20/200 | +0.5% | 0.38 | 1 | +3.8% |
| 2006 | 20/200 | +4.4% | 1.93 | 1 | +11.8% |
| 2007 | 10/100 | +1.1% | 0.24 | 3 | +2.2% |
| 2008 | 10/100 | −2.4% | −0.81 | 3 | −35.6% |
| 2009 | 10/200 | +8.5% | 1.32 | 3 | +20.2% |
| 2010 | 10/100 | +4.2% | 0.73 | 2 | +11.0% |
| 2011 | 20/100 | −2.1% | −0.59 | 4 | −1.1% |
| 2012 | 20/100 | −0.7% | −0.20 | 5 | +14.5% |
| 2013 | 50/100 | +10.0% | 1.74 | 2 | +25.5% |
| 2014 | 50/100 | 0.0% | 0.00 | 0 | +12.4% |
| 2015 | 50/100 | +0.5% | 0.27 | 4 | +1.2% |
| 2016 | 50/100 | −0.2% | −0.03 | 5 | +11.2% |
| 2017 | 50/200 | 0.0% | 0.00 | 0 | +19.4% |
| 2018 | 50/200 | 0.0% | 0.00 | 0 | −7.5% |
| 2019 | 20/50 | +5.2% | 1.05 | 6 | +33.1% |
| 2020 | 20/50 | +8.7% | 1.81 | 3 | +16.1% |
| 2021 | 20/50 | +1.9% | 0.66 | 2 | +28.8% |
| 2022 | 10/100 | −4.1% | −1.19 | 6 | −21.1% |

Aggressive sizing (`RISK_PER_TRADE_PCT=0.02 MAX_POSITION_PCT=1.0`): full period
+158.4% vs +160.0% buy-and-hold, Sharpe 0.44 vs 0.31, max DD −26.7% vs −56.8%,
**but the 20% kill switch tripped** (in the 2020 crash) and the run was flat
afterwards; 21 daily-loss halts. Walk-forward OOS: +103.0%, Sharpe 0.50, max DD
−16.9%.

Reading: the classic result. Trend-following on the index cuts drawdown by a lot
(−9% vs −57%) and modestly improves Sharpe (0.60 vs 0.47), at the price of a much
lower return because it is out of the market a lot and misses rebounds (2009,
2013, 2019–2021). Three test years produced zero trades because the selected
parameters never crossed. The chosen parameters jump around from fold to fold
(50/100 → 20/200 → 10/100 → 20/50), which is what parameter instability looks
like. Fifty-three trades over 20 years is too few to distinguish skill from luck.

### 3.2 `mean_reversion` on SP500, 2000-01-03 → 2022-12-28

`python -m bot backtest --strategy mean_reversion --symbol SP500 --start 2000-01-03 --end 2022-12-28`

| | Full period, fixed 20/2.0/0.5 | Walk-forward OOS (2003–2022) | Buy & hold (same OOS window) |
|---|---|---|---|
| Total return | +2.1% | +36.1% | +318.5% |
| CAGR | +0.09% | +1.56% | +7.43% |
| Sharpe | 0.04 | 0.45 | 0.47 |
| Ann. volatility | 3.5% | 3.6% | 19.2% |
| Max drawdown | −14.0% | −12.6% | −56.6% |
| Trades | 150 | 156 | – |
| Win rate | 59% | 63% | – |
| Profit factor | 1.03 | 1.57 | – |
| Time in market | 12% | 14% | – |
| Costs paid | $3,995 | – | – |

Reading: with fixed default parameters it is a coin flip after costs (profit
factor 1.03; costs ate $4k of $6k gross). Walk-forward parameter selection helps
(Sharpe 0.45), but the result is not better than buy-and-hold on a risk-adjusted
basis and the worst fold (2008: −10.9%, Sharpe −1.88) is exactly the "catching a
falling knife" failure mode of dip-buying. The aggressive variant tripped the kill
switch in the 2008 test fold. 2008 alone would have ended a real account's year.

### 3.3 Both strategies on GOOG (real OHLCV), 2005-06-01 → 2013-03-01

`python -m bot backtest --strategy ma_crossover --symbol GOOG --start 2005-06-01 --end 2013-03-01`
`python -m bot backtest --strategy mean_reversion --symbol GOOG --start 2005-06-01 --end 2013-03-01`

| | MA full | MA OOS (2008–2013) | MR full | MR OOS (2008–2013) | B&H full | B&H OOS |
|---|---|---|---|---|---|---|
| Total return | +10.1% | +2.8% | +6.4% | +2.6% | +179.9% | +41.4% |
| CAGR | +1.25% | +0.58% | +0.81% | +0.54% | +14.2% | +7.6% |
| Sharpe | 0.24 | 0.16 | 0.40 | 0.20 | 0.57 | 0.39 |
| Max drawdown | −17.2% | −7.9% | −3.5% | −5.4% | −65.3% | −56.1% |
| Trades | 11 | 15 | 44 | 45 | – | – |
| Profit factor | 2.49 | 1.37 | 1.43 | 1.15 | – | – |

Reading: positive but tiny, far below buy-and-hold, and with 11–15 trades the MA
result is statistically meaningless. This is the only dataset with real opens, and
it confirms the close-only runs are not hiding a fill-price problem.

### 3.4 Five-stock basket (AAPL, MSFT, JPM, XOM, KO), 2000 → 2022

`python -m bot backtest --strategy ma_crossover --symbol AAPL --symbol MSFT --symbol JPM --symbol XOM --symbol KO --start 2000-01-03 --end 2022-12-28` (and `mean_reversion`)

| | MA full (50/200) | MA OOS | MR full | MR OOS | B&H OOS |
|---|---|---|---|---|---|
| Total return | **−18.3%** | +1112.8% | **−11.3%** | +83.1% | +3053% |
| CAGR | −0.87% | +13.3% | −0.52% | +3.1% | +18.9% |
| Sharpe | −0.28 | 1.10 | −0.20 | 0.40 | 0.92 |
| Max drawdown | −20.5% | −15.4% | −21.2% | −26.7% | −48.4% |
| Trades | 21 | 229 | 31 | 422 | – |
| Kill switch | tripped 2001-03 | – | tripped | 1 fold (2020) | – |

Reading: both full-period runs **lost money** because the 20% kill switch tripped
during the 2000–2002 bear market and the bot then sat in cash for two decades;
that is the kill switch doing exactly what it is configured to do, and it shows
why "kill switch at −20% then never trade again" is not a complete policy — a real
deployment needs a documented, human re-entry decision. The walk-forward numbers
look impressive only because each fold restarts with fresh risk state and because
the basket is survivorship-biased (holding AAPL from 2003 makes anything look
good). Do not read +1112% as evidence of anything.

## 4. Verdict

**Neither strategy has demonstrated an edge worth capital.** Specifically:

- `ma_crossover` on the index proxy is the textbook "lower drawdown, lower return,
  slightly better Sharpe" trend filter. Out of sample it made +2.3%/year vs +7.4%
  for buy-and-hold, with a third of the volatility. That is a defensible *risk
  preference*, not alpha, and 53 trades in 20 years cannot support a claim either way.
- `mean_reversion` is break-even after costs with fixed parameters, mildly positive
  with walk-forward parameters, and structurally exposed to crashes (2008 fold).
  On single stocks it lost money and hit the kill switch.
- Every run underperformed buy-and-hold on raw return. Two of the eight full-period
  runs lost money outright.

**Does anything deserve paper trading?** Yes, but as a *systems* test, not as a
bet: run `ma_crossover` on SPY (and optionally QQQ) in paper mode with the default
risk settings. It trades rarely, its behaviour is predictable, and it will
exercise every part of the pipeline (calendar handling, OPG submission, fills,
stops, state recovery, dashboard, alerts) with real Alpaca data over a few
months. Judge the *infrastructure* on that run (missed sessions, duplicate orders,
reconciliation mismatches, slippage vs the 3 bps assumption), not the P&L.
`mean_reversion` should not be paper traded on single stocks with these settings;
on SPY it is acceptable as a second pipeline test if you accept that it will
probably be flat-to-slightly-positive.

What would have to be true before either deserved real money: a backtest on the
actual instruments (SPY/QQQ from Alpaca, with real opens) showing a walk-forward
Sharpe advantage over buy-and-hold that survives 2× the assumed costs; a
parameter surface that is smooth rather than the fold-to-fold jumping seen above;
at least a few hundred out-of-sample trades; and a written policy for what happens
after the kill switch trips.

## 5. Things to do next (in order)

1. Run `python -m bot data fetch --symbol SPY --symbol QQQ --start 2015-01-01 --end 2025-12-31`
   on a machine with Alpaca access, re-run the four SP500 commands above on SPY,
   and update this report. The engine is data-source agnostic; nothing else changes.
2. Start `python -m bot paper --strategy ma_crossover --symbol SPY --symbol QQQ` and let
   it run through at least one signal change. Compare actual fills with the backtest's
   next-open assumption.
3. Add a regime/volatility filter and a re-entry policy after the kill switch; both
   are the obvious weaknesses exposed above.
4. Only then consider intraday bars.
