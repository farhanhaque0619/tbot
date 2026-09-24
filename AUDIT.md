# AUDIT — state of the repository before Phase 1

Audited commit: `8dfbe07` on `claude/sweet-heisenberg-x2zz32` (three commits, 49 tracked files,
~3,000 lines under `bot/`). Audit date: 2026-09-24. Nothing was changed before this audit was written.

## Checks run

| Check | Result |
|---|---|
| `git status` | clean |
| `git log --oneline --decorate -20` | 3 commits, branch tracks origin |
| `python -m pytest` | 38 passed, all offline, ~10 s |
| Static checks | none configured. `ruff --select E,F,W,B,UP` run ad hoc: 39 findings, all cosmetic (unused imports, one-line statements, `zip` without `strict`, quoted annotations). No logic findings. |
| Secret scan | `git grep` over working tree and `git log -p --all` for Alpaca key shapes (`AK…`, `PK…`, 40-char secrets): none, other than the obvious placeholder `PKTESTKEY123` in a unit test. `.env` does not exist. `.gitignore` ignores `.env`, `*.env`, `state/`, `logs/`, `data_cache/`, `reports/*.csv|json`. |
| Dependency audit | `pip-audit -r requirements.txt`: one advisory, `python-dotenv 1.1.1` (PYSEC-2026-2270, fixed in 1.2.2). Bump planned in Phase 1. |
| Smoke tests | Without keys: `status` and `paper` fail with a clear message; `risk show`, `data list` work. With `LIVE_TRADING=true`: `paper` refuses without `--i-understand-live-trading`; with the flag but no TTY it refuses ("needs an interactive terminal"). |
| Network | `paper-api.alpaca.markets`, `api.alpaca.markets`, `data.alpaca.markets` all unreachable from this container (egress policy). No Alpaca call of any kind has ever been made by this code. |

**Credential incident.** A live key/secret pair was pasted into the chat that requested this work. Chat
transcripts are stored; treat that pair as compromised and **rotate it in the Alpaca dashboard now**. It has
not been written to any file, log, or commit, and this container cannot reach Alpaca anyway.

## The 43 questions

**1. What exactly does the bot currently trade?** Nothing has ever been traded. The code can trade US equities/ETFs
via the Alpaca `TradingClient`, whole shares only, one market order per symbol per completed daily session.
The only execution path ever exercised is the in-memory `FakeBroker`.

**2. Which asset classes are supported?** US equities and ETFs (whatever `StockHistoricalDataClient` /
`TradingClient.submit_order` accept as a stock symbol). No crypto, options, or futures code exists.

**3. Which symbols are configured by default?** None. Every command requires `--symbol`. Examples in the README use
SPY and QQQ. The offline cache currently holds `SP500`, `GOOG`, 20 large caps, and 5 factor ETFs from
`scripts/load_sample_data.py` (research data, not Alpaca data).

**4. Long-only, long/flat, or long/short?** `ma_crossover`: long/flat. `mean_reversion`: long/flat by default;
`allow_short=true` enables short entries. The engine, sizer, and paper loop handle negative positions, but shorting
has never been backtested or tested against a broker.

**5. Can it hold more than one symbol simultaneously?** Yes. Multiple `--symbol` flags create one strategy instance
per symbol sharing one cash pool and one `RiskManager`; `MAX_POSITIONS` (default 5) caps concurrency.

**6. What causes `ma_crossover` to enter?** After both SMAs are full, on a bar where `fast_sma > slow_sma` and the
previously emitted target was not already 1, it emits `Signal(target=1)`. Strict `>`; equality is flat.
(`bot/strategies/ma_crossover.py`)

**7. What causes `ma_crossover` to exit?** A bar where `fast_sma <= slow_sma` after having been long emits
`target=0`. Additionally the risk layer exits on the ATR stop or the kill switch (see 19, 24), after which the
strategy resets its state and will re-emit `target=1` on the very next bar if `fast > slow` still holds.

**8. Fast MA length.** Default 50. Grid for walk-forward: 10, 20, 50.

**9. Slow MA length.** Default 200. Grid: 50, 100, 200 (only `fast < slow` combinations).

**10. Indicator implementation.** Simple moving averages of *close* via an O(1) incremental `RollingMean` (deque +
running sum). Z-score uses `RollingStats` (mean and sample std, ddof=1, recomputed over the window). ATR is Wilder's
`RollingATR` (see 17). Verified equal to pandas rolling equivalents in `test_rolling_indicators_match_pandas`.

**11. Exact behaviour during insufficient warm-up.** `on_bar` returns `None` until the slow window (MA) or the
lookback window (MR) is full. The backtester feeds `warmup` extra bars before `trade_start` and never trades them;
the CLI loads `max(grid warmups)+20` bars before `--start`. The paper loop loads `strategy.warmup + 14 + 5` bars before
the session; if fewer bars exist the strategy simply never signals (silently flat). There is no explicit
"insufficient warm-up" error or alert.

**12. What causes `mean_reversion` to enter?** With `z = (close − mean_N) / std_N` over `lookback` closes: flat and
`z < −entry_z` → long. If `allow_short`, flat and `z > entry_z` → short. `std <= 0` → no signal.

**13. What causes `mean_reversion` to exit?** Long and `z > −exit_z` → flat. Short and `z < exit_z` → flat.
Plus risk-layer exits (stop, kill switch), after which internal position resets to 0.

**14. Every lookback and threshold.** `lookback=20`, `entry_z=2.0`, `exit_z=0.5`, `allow_short=False`. Grid:
lookback {10, 20, 40} × entry_z {1.5, 2.0, 2.5} × exit_z {0.0, 0.5}. ATR period 14 (engine constant). Stop multiple
`ATR_STOP_MULT=2.0`.

**15. Can strategies short?** Only `mean_reversion` with `allow_short=true`. Nothing prevents the paper loop from
submitting a sell-to-open when that flag is set. There is no account-level `shorting_enabled` check.

**16. Position sizing.** Fixed fractional (`bot/risk/sizing.py`): `qty = floor(min(risk_pct × equity / stop_distance,
max_position_pct × equity / price, cash / price))`. Defaults: `RISK_PER_TRADE_PCT=0.01`, `MAX_POSITION_PCT=0.50`.
Whole shares; a $100 account cannot buy one share of SPY, so it would size to 0 and never trade.

**17. ATR calculation.** Wilder ATR(14): true range = max(high−low, |high−prev close|, |low−prev close|); first bar
uses high−low; seeded with the simple mean of the first 14 TRs, then `atr = (atr×13 + tr)/14`. Identical code in
the backtester and the paper loop. On close-only data, TR collapses to |close − prev close|.

**18. Stop-distance calculation.** `stop_distance = ATR_STOP_MULT × ATR` if ATR is ready, else `0.02 × price`. A
strategy may override with `Signal.stop_price`, in which case `stop_distance = |close − stop_price|`. Stop level for a
long = `close_at_signal − stop_distance`.

**19. How stops are executed.** Not broker-side. Both engine and loop check the *completed bar's close* against the
stop; if breached, an exit market order is queued for the next open. A gap through the stop fills at the next open,
not at the stop price. No intraday protection.

**20. Max position sizing.** `MAX_POSITION_PCT` (default 50% of equity per position); no leverage (long entries are
also capped by available cash). Short notional is capped by `MAX_POSITION_PCT` only; margin is not modelled.

**21. Max simultaneous positions.** `MAX_POSITIONS=5`. Counted as broker positions + pending entry orders.

**22. Max daily loss.** `DAILY_LOSS_LIMIT_PCT=0.03`: when equity falls 3% below the day's reference equity (previous
day's last mark), no *new* entries until the next day. Existing positions are not closed.

**23. Max portfolio drawdown.** `MAX_DRAWDOWN_PCT=0.20` from peak equity (peak tracked from the first mark, never
decays).

**24. Kill-switch behaviour.** On the mark that crosses the limit: backtester queues exit orders for every position at
the next open and ignores all further signals. Paper loop: `cancel_all_orders()` then `close_all_positions()`
(immediate market orders), critical Discord alert, `killed=true` persisted; every later cycle returns
`status=killed` without trading.

**25. Reset after a kill switch.** Manual only: `python -m bot risk reset`, which prompts for the word `RESET`, clears
the flag, and sets peak equity to the current equity. Nothing automatic.

**26. Backtest execution assumptions.** Event-driven daily loop: fills at bar `t+1` open for decisions made on bar
`t` close; equity marked at close; one position per symbol, full in/out; no partial fills; no leverage; positions
open at the last bar are closed at that bar's close with exit costs.

**27. Slippage assumptions.** `SLIPPAGE_BPS=2` adverse move plus half of `SPREAD_BPS=2` per side → 3 bps per side,
6 bps round trip, applied multiplicatively to the open price. Same for the buy-and-hold benchmark's entry.

**28. Commission assumptions.** `COMMISSION_PER_SHARE=0` (Alpaca is commission-free for stocks). Regulatory fees
(SEC/TAF on sells) are not modelled.

**29. Next-bar/open/close assumptions.** Signal on close(t) → fill at open(t+1). Stops checked on close(t) → exit
at open(t+1). On close-only research data open == close, so fills are effectively at close(t+1).

**30. Corporate-action handling.** Prices are requested `DATA_ADJUSTMENT=split` (split-adjusted, dividends not
included) from Alpaca. Cache invalidation: on each extension the last ~5 cached bars are re-fetched and compared;
>0.5% disagreement discards and refetches the symbol. Symbol changes, delistings, and dividends are not handled.
The paper loop never checks that a broker position's share count still matches after a split.

**31. Fractional-share handling.** None. Quantities are `int`; sizing floors to whole shares.

**32. Extended-hours handling.** Orders are never marked `extended_hours`. OPG orders fill only in the opening
auction. The "day" fallback is used only when the broker clock says the market is open.

**33. Market calendar handling.** Paper loop asks Alpaca's calendar for the last 10 days and takes the last session
whose close has passed; if the calendar call fails it falls back to weekdays 09:30–16:00 ET, which ignores holidays.
Timestamps are America/New_York throughout; daily bars are stamped at NY midnight.

**34. Broker reconciliation.** Each cycle: local positions with a filled entry but no broker position are dropped
(logged); quantity mismatches trust the broker; broker positions in configured symbols that the bot did not open are
adopted with no stop. Decisions are made as "desired exposure vs actual broker position", so a lost state file
converges. Unmanaged symbols are ignored.

**35. Client-order-ID construction.** `f"{run_id}-{symbol}-{session_date}-{entry|exit}"`, e.g.
`paper-SPY-2025-01-15-entry`. Deterministic per decision; at most one entry and one exit per symbol per session.

**36. Restart/crash behaviour.** State (`state/<run_id>.json`) is written atomically (temp file, fsync, rename)
before and after every submission and after every cycle. On restart, orders in `submitting` state are looked up by
client id: found → adopted; not found → marked `lost` and the session is re-processed. Verified by
`test_crash_after_submit_before_state_save_does_not_double_order`. Strategy state is not persisted; it is
re-derived by replaying bars, with risk-forced exits replayed from `risk_exits`.

**37. If Alpaca returns an error.** `with_retry`: 429/500/502/503/504 and connection/timeout errors are retried up to
5 times with exponential backoff and jitter (1 s → 30 s cap). Other 4xx raise immediately. A duplicate-client-id
rejection (400/422) is resolved by fetching the existing order. In loop mode an exception ends the cycle, is logged,
alerted, and the poll interval backs off up to 1 h; `--once` re-raises.

**38. If an order is partially filled.** Not handled. `partially_filled` is neither terminal nor filled, so the loop
waits; the position record keeps the requested qty until `_reconcile_positions` trusts the broker's qty. An OPG
order that partially fills and is then cancelled would hit the "canceled" branch and drop the local position record
even though shares were bought.

**39. If an order is rejected.** Terminal non-filled status → warning log + Discord alert; for an entry the local
position record is removed. The session stays marked processed, so the decision is not retried that day.

**40. If market data is stale.** If the bar for the last completed session is missing, the symbol is skipped and
retried next cycle (logged at INFO). There is no freshness check on the *account* or *quote* side, and no
stale-data alert.

**41. If internet connectivity disappears.** Requests raise `ConnectionError` → retried with backoff → cycle fails →
alert (which also fails silently) → loop keeps running with a growing sleep. Nothing is submitted while
disconnected. Positions already open are unprotected (stops are software-side).

**42. Can existing code submit live orders?** Yes, in principle: `AlpacaBroker(settings, paper=False)` creates a
live `TradingClient`, and `cmd_paper` reaches that path when `LIVE_TRADING=true` and the gates pass. There is only one
key pair (`ALPACA_API_KEY/SECRET`), so the same keys are used for paper and live — a live key in `.env` plus
`LIVE_TRADING=true` is enough. This is the main thing Phase 1 must fix.

**43. Every safeguard against accidental live execution.**
1. `LIVE_TRADING` defaults to `false`; `Settings.paper` is `not live_trading`.
2. `cmd_paper` refuses if `LIVE_TRADING=true` without `--i-understand-live-trading`.
3. It refuses without an interactive TTY, and requires typing `LIVE`.
4. `AlpacaBroker(paper=True)` raises if the SDK's base URL does not contain `paper-api`.
5. Every other command hard-codes `paper=True`.
Gaps: one shared key pair; no account-side confirmation that the connected account is really paper; no
"armed/disarmed" runtime state; no autonomous-trading flag separate from environment; no notional/exposure caps
suitable for a $100 account.

## Verdict of the audit

The core (engine, risk math, state machine, idempotent orders) is sound and tested, but the system has **never
touched a broker API**, cannot trade a $100 account (whole shares only), does not handle partial fills, and its
live/paper separation rests on a single key pair plus a flag. Phases 1–5 address exactly these.
