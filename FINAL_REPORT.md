# FINAL_REPORT — state of the system after Phases 0–19 (2026-09-24)

## 1. Repository audit
AUDIT.md (commit `d490e55`) answers the 43 questions for the pre-existing code. Headline findings that this round
fixed: one shared key pair for paper and live; whole shares only (a $100 account could never trade); no partial-fill
handling; no pre-trade gate; no account-side paper/live verification; no armed/disarmed state; stale-data handling
limited to "bar missing". Findings that remain open are listed in §19.

## 2. Exact current trading strategy in plain English
Two baselines, unchanged in logic. **`ma_crossover`**: at each daily close compute the 50-day and 200-day simple
moving averages of close; if the 50-day is above the 200-day and we are flat, buy at the next open; if it is at or
below and we are long, sell at the next open. **`mean_reversion`**: compute the z-score of today's close against
the last 20 closes; if flat and z < −2.0, buy; if long and z > −0.5, sell. Both are also exited by the risk layer
when a close breaches the 2×ATR(14) stop, and re-enter on the next bar if their condition still holds.

## 3. Exact parameters
`ma_crossover`: fast=50, slow=200. `mean_reversion`: lookback=20, entry_z=2.0, exit_z=0.5, allow_short=false.
Risk: RISK_PER_TRADE_PCT=0.01, MAX_POSITION_PCT=0.50, DAILY_LOSS_LIMIT_PCT=0.03, MAX_DRAWDOWN_PCT=0.20,
MAX_POSITIONS=5, ATR_STOP_MULT=2.0, ALLOW_FRACTIONAL=true, QTY_DECIMALS=3, MAX_SPREAD_BPS=50,
MAX_PRICE_DEVIATION_PCT=0.10, MAX_STALE_DATA_SECONDS=900. Costs (backtest): SLIPPAGE_BPS=2, SPREAD_BPS=2, no
commission. Live safe mode: $25/order, $50 gross, $5/day, $10 drawdown, 1 position, SPY/QQQ.

## 4. Exact trades it is capable of making
Market orders only, US equities/ETFs only, one entry and/or one exit per symbol per completed daily session,
long entries (`buy`) and long exits (`sell`); short entries only if `mean_reversion --param allow_short=true` AND
the account has shorting enabled AND safe mode is off. Whole-share orders are market-on-open (OPG); fractional
orders are day orders submitted while the market is open. Kill switch: `cancel_all_orders` then
`close_all_positions` at market.

## 5. Can it short? Only as above; forbidden in `SAFE_LIVE_TEST_MODE`; never backtested. Effectively no.
## 6. Can it use margin? No. Long entries are capped by cash (backtest) and, in safe mode, by cash rather than buying power. Margin multiplier is reported by `status` but never used.
## 7. Can it trade fractions? Yes (`ALLOW_FRACTIONAL=true`, rounded down to 3 decimals, `fractionable` asset check, day orders only). Required for a $100 account.
## 8. Can it trade after hours? No. Orders are never flagged `extended_hours`; safe mode rejects any TIF other than day/opg.
## 9. Can it trade crypto/options? No code path exists; `symbol_tradable` requires `asset_class == us_equity`.

## 10. All risk limits
Stateful (backtest + execution): daily loss halt (3%, and $5 in safe mode) → no new entries until next day; kill
switch (20% from peak, and $10 in safe mode) → liquidate + halt until human `risk reset`; max positions (5; 1 in
safe mode); fixed-fractional sizing with 2×ATR stop, ≤50% of equity, no leverage. Pre-trade gate
(`RiskManager.check_order`, ~25 checks, every order): account healthy · paper/live mode consistent (broker host +
account-number shape) · no duplicate client id · no open order for the symbol · qty > 0 · reference price within 10%
of quote mid · fractionable if fractional · fractional ⇒ day TIF · symbol tradable US equity · strategy state valid
(exit ≤ position, right side; no entry on top of a position) · kill switch clear · daily loss ok · data fresh (bar
current; quote ≤ 900 s for day orders) · market open (or OPG) · spread ≤ 50 bps · optional realised-vol cap ·
position limit · order notional ≤ 50% equity · buying power (cash in safe mode) · no short unless allowed · safe:
symbol allow-list, fractionable-only, $25/order, $50 gross, $5 daily, $10 drawdown, day/opg only. Live interlock
(all must pass): live credentials, TRADING_ENV=live, --live flag, armed with typed phrase + acknowledged limits (30
min TTL, cleared on restart, invalidated if limits change), account not paper, trading_blocked=false,
account_blocked=false + ACTIVE, data fresh, risk manager healthy, kill switch clear.

## 11. Paper Alpaca integration status
**Not run.** The build container cannot reach `paper-api.alpaca.markets`. The integration suite
(`tests/integration/test_alpaca_paper.py`: account/env, clock/calendar, bars/quote/asset, submit/read/duplicate/
cancel, rejections, Trader restart safety) is written and gated behind `RUN_ALPACA_INTEGRATION=1` (+
`RUN_ALPACA_INTEGRATION_ORDERS=1` for order submission). Everything in the execution path is unit-tested against
`FakeBroker` (partial fills, rejections, cancels, network errors, 429 retry, restarts around orders and fills, stale
and corrupt state, holidays/weekends, stale quotes, missing bars, insufficient warm-up, insufficient buying power,
invalid symbol). Expect SDK field-shape surprises on first real contact; `doctor --paper` is the first command to run.

## 12. Live Alpaca read-only status
**Not run** (no network). `python -m bot status --live` and `doctor --live` are implemented and read-only.
A live key pair was pasted into the chat that commissioned this work; it was not stored anywhere and must be
rotated. No `.env` exists in this checkout; `git log -p --all` contains no key-shaped strings.

## 13. Historical-data source status
Alpaca provider implemented (split-adjusted, SIP→IEX fallback, 15-minute free-plan rule, cache-extension with split
detection) but **never exercised**. All reported numbers use bundled research datasets: S&P 500 index and 20 stocks
close-only 1990–2022, GOOG OHLCV 2004–2013. `python -m bot data check` reports quality. DATA.md documents
adjustments, dividends, symbol changes, gaps, time zones, holidays, extended hours, feed, lookahead.

## 14. Backtest results (S&P proxy 2000–2022, fixed params, 3 bps/side, default risk)
ma_crossover: +95.7% total, CAGR +3.0%, Sharpe 0.51, MaxDD −14.9%, 26 trades, 65% in market, costs $973.
mean_reversion: +2.0%, CAGR +0.1%, Sharpe 0.04, MaxDD −14.2%, 150 trades, 12% in market, costs $4,074.
Buy & hold: +160%, CAGR +4.2%, Sharpe 0.31, MaxDD −56.8%. GOOG 2005–2013: MA +10.2% (Sharpe 0.24, 11 trades), MR
+6.5% (0.40, 44 trades), B&H +180% (0.57). Five-stock basket: both baselines tripped the kill switch in 2001 and
lost 18%/11% over the full period.

## 15. Walk-forward results (3y/1y, 20 folds, S&P proxy)
ma_crossover +53.0% / CAGR +2.2% / Sharpe 0.55 / MaxDD −8.9% / 56 trades. mean_reversion +36.8% / +1.6% / 0.45 /
−12.9% / 156. Research candidates: breakout 0.38, buffered MA 0.72 (30 trades), vol-filter 0.49 (and −0.43 on
GOOG). Parameter selection jumped across folds for every strategy.

## 16. Comparison against buy-and-hold
Buy-and-hold over the same OOS window: +318.5%, CAGR +7.4%, Sharpe 0.47, MaxDD −56.6%. Every strategy underperforms
on return (they are 30–65% invested); MA-type strategies win on Sharpe and drawdown by being flat in bear regimes
(602 days) and lose in sideways regimes (3805 days: ≈0% vs +11.7%/yr). Mean reversion is worse than buy-and-hold
on Sharpe and negative in bear/high-vol regimes.

## 17. Known bugs fixed (this round)
- A stop exit already queued for a bar was overwritten by a same-bar strategy exit (mis-attributed exit reason,
  strategy not notified). Fixed; changed reported numbers by < 0.5%.
- Harness buy-and-hold fired its only signal in the warm-up window and never traded (harness-only, caught by
  reviewing output).
- Whole-share sizing on a small account produced 0 shares silently; now fractional with explicit rounding, asset
  fractionability check, and a decision record saying "sized to zero".
- Partial fills were not booked; a partially filled then cancelled entry dropped the local position although shares
  were held. Fixed and tested.
- Corrupt state file crashed the process; now moved aside and recovered from broker state.
- Previous round (kept): double-counted entry slippage; ATR seeding mismatch; timestamp unit mismatch; sign error
  in end-of-run liquidation.

## 18. New tests added (88; total 126 offline + 7 integration)
Credential shape and slot mismatch; missing credentials; broker host selection without network; env mismatch
broker↔trader and state file↔env; live loop refused without LIVE_AUTONOMOUS_TRADING; interlock arm requirements,
expiry, fingerprint invalidation, clear-on-startup; live trader starts disarmed and blocks; armed trader submits a
fractional day order within caps; safe mode blocks second position/other symbols; dollar kill switch; corrupt state
recovery; every `check_order` check failing alone (24 parametrised cases) plus exits-allowed-when-killed, exit
validation, OPG freshness rule, margin/short follow the account; fractional rounding; partial fill → fill; partial
fill → cancel; rejection; insufficient buying power; canceled entry; invalid symbol; network failure + recovery;
429/5xx retry vs 4xx; restart while pending; restart after fill; lost state adopts broker positions; weekend/holiday
session resolution; missing bar waits; insufficient warm-up; stale quote blocks day orders; malformed quote
tolerated; multi-symbol future mutation; loop decision unchanged by future bars; MarketState causal; data quality
flags; backtest survives split + gap; harness cuts/regimes/walk-forward; causal regime labels; parameter surface
fragility; candidates run; advisor shadow mode has zero effect; advice vocabulary closed; Jev adapter never raises
and is off by default; Kelly bounded and research-only; review is read-only; four architecture import-scan tests.

## 19. Remaining risks
1. **Nothing has touched a real broker.** SDK field shapes, OPG acceptance windows, fractional minimums ($1 notional),
   free-plan quote availability, and calendar edge cases are all untested against Alpaca.
2. **Stops are software-side and daily.** An overnight gap through the stop is fully borne; an outage leaves
   positions unprotected. Safe-mode caps bound the damage to ~$10–25.
3. **Kill-switch re-entry policy is "human decides".** The basket backtests show what "halt forever" costs.
4. **Reconciliation adopts unknown positions without a stop.** Documented; means a manually opened position is
   managed by strategy exits only.
5. **Paper/live account-number heuristic (`PA` prefix)** and key-prefix heuristic (`PK`/`AK`) are Alpaca conventions,
   not guarantees. `STRICT_KEY_PREFIX_CHECK` can be disabled; the base-URL check cannot.
6. **Strategy evidence is weak and on proxy data.** See §14–16. Anything positive is within selection noise.
7. **Time-of-day dependence.** Whole-share OPG orders need the process to run between 19:00 and 09:28 ET; fractional
   orders need it during market hours. A cron misconfiguration silently produces `waiting` records (the review
   flags this).
8. **Jev integration is a generic HTTP contract**, written without access to Jev's documentation; it will need
   adapting, and it is disabled.
9. **Single-process assumption.** Two processes on the same run id could race on the state file (the broker's
   duplicate-id rejection is the backstop).

## 20. Exact commands to run next (on your machine, in this order)
```bash
# 0. rotate the pasted live key pair in the Alpaca dashboard; put new keys in .env via an editor
git checkout claude/sweet-heisenberg-x2zz32 && python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && python -m pytest -q
python -m bot doctor --paper                                     # 1. connectivity, credentials, freshness
python -m bot data fetch --symbol SPY --symbol QQQ --start 2015-01-01 --end 2026-09-24
python -m bot data check --symbol SPY --symbol QQQ               # 2. real data quality
python -m bot backtest --strategy ma_crossover --symbol SPY --start 2015-01-01 --end 2026-09-24
python -m bot research compare --symbol SPY --start 2015-01-01 --end 2026-09-24   # 3. real-data evidence
RUN_ALPACA_INTEGRATION=1 python -m pytest tests/integration -q -s                 # 4. read-only paper integration
RUN_ALPACA_INTEGRATION=1 RUN_ALPACA_INTEGRATION_ORDERS=1 python -m pytest tests/integration -q -s   # ~$1.50 paper orders
python -m bot paper --strategy ma_crossover --symbol SPY --once   # 5. at ~19:30 ET, then again: second run must not order
python -m bot dashboard && python -m bot review --run-id paper
python -m bot status --live && python -m bot live check          # 6. read-only live; expect 'not armed' only
# 7. then, and only then, LIVE_RUNBOOK.md steps 12–21
```

## WHAT COULD I BE WRONG ABOUT?
- **That the fake broker resembles Alpaca.** It was written from the SDK's type signatures, not from observed
  behaviour. Order status vocabularies, partial-fill semantics for OPG, `qty_available`, and the 403 for insufficient
  buying power are all guesses until the integration suite runs.
- **That OPG is the right default.** If Alpaca paper rejects OPG for any reason, the loop falls back to day orders at
  the open, which changes fill assumptions versus the backtest.
- **That the paper account number starts with `PA` and live keys with `AK`.** If Alpaca changes conventions, the env
  check produces false mismatches (safe direction) — or, worse, if a live account number ever started with `PA`, a
  false match. The base-URL check is the one that cannot be fooled.
- **That the close-only proxy tells us anything about SPY.** Real opens, dividends, and an ETF's tracking could move
  every number; the qualitative regime story (flat in bears, zero in sideways) is the part I would expect to survive.
- **That 2×ATR is merely "tight".** It might be the main reason the strategies show low drawdown; widening it (H4)
  could remove the only thing they have going for them. It has not been tested.
- **That "no lookahead" is complete.** The engine is tested by mutating future bars, but the *regime labels* in the
  harness use a full-sample volatility quantile (documented), and the *walk-forward* uses full-sample grid choices
  made by me. Candidate strategies were designed with knowledge of the period they were tested on.
- **That the risk gate is exhaustive.** It has 25 named checks; it does not check Alpaca's own constraints (PDT rule
  after 3 day trades in 5 days on < $25k, which a daily strategy will not hit but a stop-and-re-enter sequence could),
  the $1 minimum fractional notional (a $0.05 × $600 order is fine; a smaller account might not be), or settlement.
- **That the security of `.env` is sufficient.** Keys in a plaintext file on a laptop are a common compromise
  vector; the repo can only ensure they are not in git or logs.
- **That anyone will read the runbook.** The interlock is designed so that skipping steps fails closed, but a
  determined operator can set `LIVE_AUTONOMOUS_TRADING=true` and `SAFE_LIVE_TEST_MODE=false` in thirty seconds.
