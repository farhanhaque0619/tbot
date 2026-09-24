# tbot — research-grade trading bot: backtests, Alpaca paper trading, gated live execution

> **PAPER IS DEFAULT. LIVE TRADING USES REAL MONEY. BACKTEST PERFORMANCE DOES NOT GUARANTEE FUTURE PERFORMANCE.**

## What this is

A small, deterministic Python 3.11 system that (1) backtests daily-bar strategies with an event-driven engine that
cannot look ahead, (2) paper-trades them through the Alpaca API with crash-safe state and idempotent orders, and
(3) can execute live only through a multi-gate interlock with hard dollar caps sized for a ~$100 validation account.
A separate research layer compares strategies against cash and buy-and-hold and can never place an order.

## What this is not

- Not a profitable strategy. Two baselines ship (`ma_crossover`, `mean_reversion`); neither has demonstrated edge
  (see **Evidence** below and REPORT.md / RESEARCH.md / STRATEGY_SPEC.md).
- Not an AI trader. A shadow-mode advisor interface exists (`ENABLE_JEV=false`) whose output is logged and has no
  code path to orders. Kelly sizing exists as a research function only (`ENABLE_KELLY=false`).
- Not intraday, not options, not crypto, not margin, not short (in safe mode). Daily bars, US equities, long/flat.
- Not tested against a real broker yet: as of 2026-09-24 the code has only run against the in-memory fake broker,
  because the build environment cannot reach Alpaca. The first thing to do is Phase 3 of this README.

## Strategies

| Name | Rule (long/flat) | Params | Status |
|---|---|---|---|
| `ma_crossover` | long when SMA(close, 50) > SMA(close, 200), else flat | fast=50, slow=200 | baseline |
| `mean_reversion` | long when z-score of close vs 20-day mean < −2.0, exit when z > −0.5 | lookback=20, entry_z=2.0, exit_z=0.5, allow_short=false | baseline |
| research candidates (`donchian_breakout`, `ma_crossover_buffered`, `trend_vol_filter`) | see research/HYPOTHESES.md | | research only, not executable |

Both baselines are sized by the risk layer (1% of equity at risk against a 2×ATR(14) stop, ≤50% of equity per
position, no leverage) and exited by it when the stop is breached on a daily close. STRATEGY_SPEC.md has the exact
rules, holding periods, turnover, failure regimes and parameter surfaces.

## Evidence

**Exists** (REPORT.md, RESEARCH.md; S&P 500 index proxy 2000–2022 close-only and GOOG 2005–2013 OHLCV, from bundled
research datasets, 3 bps/side costs, walk-forward 3y/1y × 20 folds):

| | ma_crossover | mean_reversion | buy & hold |
|---|---|---|---|
| Walk-forward OOS CAGR (S&P proxy) | +2.2% | +1.6% | +7.4% |
| Walk-forward OOS Sharpe | 0.55 | 0.45 | 0.47 |
| Walk-forward OOS max drawdown | −8.9% | −12.9% | −56.6% |
| OOS trades over 20 years | 56 | 156 | – |
| Parameter surface | stable | **fragile** | – |

**Does not exist:** any result on SPY/QQQ from Alpaca; any real fill; any live or paper order; any evidence of
alpha; any evidence that the 2×ATR stop is the right stop (it produces most exits — research/HYPOTHESES.md H4).

## Setup and credentials

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # edit with an editor; never echo keys into a terminal
python -m pytest -q             # 126 offline tests, ~1 min; 7 integration tests skip without credentials
```

Paper keys: Alpaca dashboard → *Paper Trading* → *API Keys* → into `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_SECRET_KEY`
(they start with `PK`). Live keys → `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_SECRET_KEY` (start with `AK`). The bot refuses
a key in the wrong slot. `.env`, `state/`, `logs/`, `data_cache/` are git-ignored; keys are `SecretStr` and a log
filter redacts them; `python -m bot doctor` reports only *presence*, never values. If a key was ever pasted into
a chat or screenshot, rotate it.

## Backtest

```bash
python -m bot backtest --strategy ma_crossover --symbol SPY --start 2015-01-01 --end 2025-12-31
python -m bot backtest --strategy mean_reversion --symbol SPY --symbol QQQ --start 2016-01-01 --end 2025-12-31 --param lookback=20
python -m bot research compare --symbol SPY --start 2015-01-01 --end 2025-12-31     # cash / B&H / baselines / candidates, cuts, regimes, walk-forward
python -m bot research surface --strategy ma_crossover --symbol SPY --start 2015-01-01 --end 2025-12-31
python -m bot data check --symbol SPY                                                # gaps, splits, duplicates, close-only
```
Bars come from Alpaca (split-adjusted, SIP with IEX fallback) and are cached in DuckDB; DATA.md explains
adjustments, holidays, time zones and lookahead protection. Without credentials: `python -m bot data import` (CSV) or
`python scripts/load_sample_data.py` (bundled research datasets).

## Paper trading (default)

```bash
python -m bot doctor --paper                                    # connectivity + config health, read-only
python -m bot status --paper                                    # account, positions, orders, clock, quote freshness
python -m bot paper --strategy ma_crossover --symbol SPY --once  # one decision cycle
python -m bot paper --strategy ma_crossover --symbol SPY         # loop (POLL_INTERVAL_SECONDS)
python -m bot dashboard --watch 30
python -m bot review --run-id paper                              # post-session report with proposals
```
Cycle: sync fills → account/positions (broker is authoritative) → risk manager → last completed session → replay
strategy → desired exposure vs actual → `RiskManager.check_order` → at most one market order per symbol per session
with a deterministic client id (`paper-SPY-2025-01-15-entry`) → decision record in `logs/decisions_paper.jsonl`.
Whole-share orders go market-on-open (19:00–09:28 ET); fractional orders (`ALLOW_FRACTIONAL=true`) go as day orders
while the market is open. A restart cannot double-order: state is written before and after submission, and the
broker refuses duplicate client ids. Real-broker integration tests: `RUN_ALPACA_INTEGRATION=1 python -m pytest tests/integration -s`.

## Live trading (real money)

Every one of these is required for a live order; any failure means no order:
`ALPACA_LIVE_*` keys · `TRADING_ENV=live` · `--live` on the CLI · `python -m bot live arm` (typed
`I UNDERSTAND THIS USES REAL MONEY` + `ACK` of the safe-mode table; expires after 30 min and on every restart) ·
account endpoint confirms a non-paper account · `trading_blocked=false` · `account_blocked=false` · fresh market data
· risk manager healthy · kill switch clear · `SAFE_LIVE_TEST_MODE` caps ($25/order, $50 gross, $5/day, $10 drawdown,
1 position, SPY/QQQ only, no shorts/margin/extended hours). The multi-cycle live loop additionally needs
`LIVE_AUTONOMOUS_TRADING=true`, which is `false` by default and which this repo never sets.

```bash
python -m bot doctor --live      # read-only
python -m bot live check         # read-only: prints all gates
python -m bot live arm / disarm
python -m bot trade --live --strategy ma_crossover --symbol SPY --once
```
Follow LIVE_RUNBOOK.md for the first controlled trade. Do not skip the paper phase.

## Risk limits and the kill switch

`RISK_PER_TRADE_PCT` (1%), `MAX_POSITION_PCT` (50%), `DAILY_LOSS_LIMIT_PCT` (3% → no new entries today),
`MAX_DRAWDOWN_PCT` (20% from peak → liquidate everything, halt), `MAX_POSITIONS` (5), `ATR_STOP_MULT` (2.0), plus the
dollar caps above in live safe mode. The same `RiskManager` runs in backtests and execution; nothing (strategy,
advisor, operator flag) can bypass `check_order`. **When the kill switch trips:** you get a critical Discord alert
(if configured), all positions are closed, every later cycle returns `status=killed`. Investigate the cause
(`python -m bot review`, `logs/`), then — and only by a human — `python -m bot risk reset --run-id <paper|live>`.

## Shutting everything down

`Ctrl-C` the loop (it finishes the current cycle) · `python -m bot live disarm` · optionally close positions in
the Alpaca dashboard (the bot reconciles and drops its local record) · set `LIVE_AUTONOMOUS_TRADING=false`.

## Layout

```
bot/config.py           settings, separate paper/live credentials, safety flags
bot/data/               Alpaca + CSV providers, DuckDB cache, calendar, quality checks
bot/strategies/         Strategy interface, the two baselines, indicators
bot/backtest/           engine (no lookahead), costs, metrics, walk-forward, reports
bot/risk/               RiskLimits, SafeLiveLimits, RiskManager (stateful limits + check_order gate), sizing
bot/execution/          broker (Alpaca / fake), Trader loop, interlock, MarketState, state store
bot/monitoring/         JSON logging with redaction, decision records, dashboard, Discord alerts
bot/research/           harness, surfaces, candidates, review, Kelly (research-only)     ← brain, cannot order
bot/advisor/            shadow-mode advisor interface (rule baseline, Jev adapter)
tests/                  126 offline tests; tests/integration/ real paper API (opt-in)
AUDIT.md · REPORT.md · RESEARCH.md · STRATEGY_SPEC.md · DATA.md · ARCHITECTURE.md · LIVE_RUNBOOK.md · FINAL_REPORT.md
```
