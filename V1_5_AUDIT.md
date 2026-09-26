# V1_5_AUDIT — Phase 0 (no code changes)

Audited commit: `2cb338a` on `claude/sweet-heisenberg-x2zz32`, 2026-09-26. Inputs: `V1_5_CODE_PROMPT.md` (authoritative),
`V1_5_RESEARCH_REPORT.md`, every file under `bot/`, `tests/`, `research/`, `scripts/`, and every root `*.md`.

## 1. What exists

| Area | Facts confirmed from code |
|---|---|
| Size | `bot/`: 5,920 lines in 40 modules. `tests/`: 2,156 lines. |
| Tests | 176 offline tests pass in ~55 s. 8 integration tests under `tests/integration/` are gated by `RUN_ALPACA_INTEGRATION=1` (4 read-only) and additionally `RUN_ALPACA_INTEGRATION_ORDERS=1` (4 that submit paper orders). Operator's last real runs: `....ssss` (read-only green) and a full closed-market `python -m bot smoke --paper` PASS. |
| Order types the broker layer supports | `submit_market_order` only (market, TIF `opg` or `day`), plus `cancel_order`, `cancel_all_orders`, `close_all_positions`. No limit, stop, bracket/OTO, replace, or `get_orders_since`. Fractional quantities are forced to `day`. |
| State persistence | `bot/execution/state.py`: one JSON file per run id, written via temp file + `fsync` + `os.replace`. A corrupt file is moved aside and rebuilt from broker state. Fields: `last_processed`, `orders`, `positions` (software stop), `risk`, `risk_exits`, `trades`, `equity_log`. Single-process assumption. |
| Restart during submission | `Trader._submit` writes the order record with status `submitting` and persists **before** calling the broker, then persists again after. On the next `_sync_orders`, a `submitting` record is looked up by client id: found → adopted; not found → marked `lost` and the session is re-opened for decision. Alpaca rejects duplicate client ids (confirmed on the real paper API 2026-09-26: resubmission returned the same order id). |
| ATR stop, backtest | `engine.py`: stop level = signal-bar close − `ATR_STOP_MULT × ATR(14)`; checked against each completed bar's **close** (step 4 of the loop); breach queues an exit at the **next open** with `risk_exit=True`, which triggers `Strategy.on_position_closed`. A stop exit already queued wins over a same-bar strategy exit. |
| ATR stop, execution | `paper_loop.py::_decide`: the stop is stored in the state's position record at entry; each cycle compares the last completed session's close against it; a breach sets `desired=0` and appends the session date to `risk_exits`, which the strategy replay consumes. No broker-side stop exists anywhere. |
| Regime labels | `bot/research/harness.py::classify_regimes`: trend labels use trailing 126-day return (causal); volatility labels use a **full-sample quantile** of trailing 63-day vol (documented leakage, to be fixed in Phase 3 per spec §11). |
| Can research/advisor reach order submission? | No. `grep` for `submit_`, `close_all`, `cancel_` under `bot/research` and `bot/advisor` returns nothing; `tests/test_architecture.py` enforces by AST import scan that those packages never import `bot.execution.broker`, `paper_loop`, `interlock`, or `alpaca`. |
| Safety properties to preserve (spec rule 5) | Separate `ALPACA_PAPER_*`/`ALPACA_LIVE_*` with `PK`/`AK` prefix checks and host verification both ways; `TRADING_ENV`; `--live`; `live arm` with typed phrase, 30-min TTL, safe-limits fingerprint, cleared on every process start; `LIVE_AUTONOMOUS_TRADING=false`; `SafeLiveLimits` ($25/order, $50 gross, $5/day, $10 drawdown, 1 position, SPY/QQQ); deterministic client ids; state before/after submission; broker-as-authority reconciliation; log redaction (type-preserving since `824afe1`); research-cannot-order; advisor shadow only. |

## 2. Frozen baselines (must reproduce after every phase)

### 2.1 Reproduced by the operator on real Alpaca SPY data (2026-09-26, cache 2016-01-04 → 2026-09-25)

`python -m bot backtest --strategy ma_crossover --symbol SPY --start 2017-01-03 --end 2026-09-26`
(3 bps/side, RISK_PER_TRADE_PCT=0.01, MAX_POSITION_PCT=0.50, ATR_STOP_MULT=2.0, ALLOW_FRACTIONAL=true, cash 100,000)

| Metric | Full period 2017-01-03 → 2026-09-25 | Buy & hold | Walk-forward OOS 2020-01-03 → 2026-09-25 | Buy & hold (OOS) |
|---|---|---|---|---|
| Final equity | 129,388.49 | 342,658.49 | 112,742.99 | 239,658.95 |
| Total return | +29.39% | +242.61% | +12.74% | +139.66% |
| CAGR | +2.69% | +13.50% | +1.80% | +13.88% |
| Sharpe | 0.57 | 0.80 | 0.65 | 0.76 |
| Sortino | 0.54 | 0.97 | 0.58 | 0.94 |
| Ann. volatility | 4.91% | 17.92% | 2.83% | 19.79% |
| Max drawdown | −11.26% | −34.18% | −5.58% | −34.18% |
| Calmar | 0.24 | 0.40 | 0.32 | 0.41 |
| Trades | 10 | – | 15 | – |
| Win rate | 50.00% | – | 53.33% | – |
| Profit factor | 5.91 | – | 2.75 | – |
| Avg trade P&L | 2,938.85 | – | 830.22 | – |
| Net trade P&L | 29,388.49 | – | 12,453.27 | – |
| Time in market | 62.51% | – | 41.63% | – |
| Costs paid / orders / daily halts | 187.34 / 20 / 1 | | | |

Walk-forward folds (train 3y / test 1y, selected by train Sharpe, min 5 trades):

| Test year | Params | Train Sharpe | Test return | Test Sharpe | Test MaxDD | Trades | B&H |
|---|---|---|---|---|---|---|---|
| 2020 | fast=20, slow=50 | 0.98 | +5.3% | 1.70 | −2.7% | 3 | +16.1% |
| 2021 | fast=20, slow=50 | 0.64 | +0.2% | 0.11 | −2.6% | 2 | +28.7% |
| 2022 | fast=10, slow=100 | 1.01 | −4.6% | −1.93 | −4.6% | 5 | −19.9% |
| 2023 | fast=20, slow=100 | 0.58 | +3.6% | 0.88 | −3.1% | 3 | +24.1% |
| 2024 | fast=10, slow=200 | 0.31 | +0.0% | 0.00 | 0.0% | 0 | +24.7% |
| 2025 | fast=10, slow=200 | 0.98 | +4.5% | 1.71 | −1.6% | 1 | +15.4% |
| 2026 (to 09-25) | fast=10, slow=200 | 1.12 | +3.4% | 1.47 | −1.5% | 1 | +12.2% |

Data: `SPY` and `QQQ` 2,698 bars each, 2016-01-04 → 2026-09-25, `source=alpaca`, split-adjusted, 102 missing weekdays
(holidays), 0 duplicates, 0 OHLC inconsistencies, `data check` OK for both. `doctor --paper` OK, exit 0.

### 2.2 Not yet frozen (the build environment has no route to Alpaca; the operator must run these once and paste the output)

```
python -m bot backtest --strategy mean_reversion --symbol SPY --start 2017-01-03 --end 2026-09-26
python -m bot backtest --strategy ma_crossover --symbol QQQ --start 2017-01-03 --end 2026-09-26
python -m bot backtest --strategy mean_reversion --symbol QQQ --start 2017-01-03 --end 2026-09-26
python -m bot research compare --symbol SPY --start 2017-01-03 --end 2026-09-26
```
Until pasted, the reproduction tests for Phase 2 use the offline proxy numbers in REPORT.md/RESEARCH.md (SP500 close-only,
GOOG OHLCV), which are in this repository's cache and are byte-reproducible here.

### 2.3 Offline proxy baselines already in the repo (reproducible in this environment)

From REPORT.md/RESEARCH.md at `2cb338a`: `ma_crossover` SP500 2000–2022 full +95.7%, Sharpe 0.51, MaxDD −14.9%, 26 trades;
walk-forward +53.0%, Sharpe 0.55, 56 trades. `mean_reversion` SP500 full +2.0%, Sharpe 0.04, 150 trades; walk-forward
+36.8%, Sharpe 0.45, 156 trades. GOOG 2005–2013: MA +10.2% / 0.24 / 11 trades; MR +6.5% / 0.40 / 44 trades.

## 3. Where the specification meets facts about this account and environment

Spec rule 7 requires stopping and writing discrepancies here before proceeding. These are not Alpaca-doc discrepancies
but constraints that change what several sections can do; each has the smallest safe change proposed.

| # | Spec item | Fact | Smallest safe change |
|---|---|---|---|
| D1 | §5.3 M1 `entry_style="cls"`, §5.5 M3 `"opg"`, §8 OTO/bracket protection | The live account (~$100) can only trade **fractional** quantities of SPY/QQQ, and fractional orders are DAY-only with no OPG/CLS/GTC/bracket/OTO (spec §8 itself). | Implement both paths as specified; the allocator's rounding rule (§6.5) selects the fractional path for this account automatically. Document that on the $100 account every overnight position is `unprotected_overnight=True` with a re-placed DAY stop, and that M1/M3 auction entries are a paper-only code path until the account can hold whole shares (SPY ≈ $770). |
| D2 | §5.4 M2 in live | One day trade per ETF per day flags a sub-$25k account under the legacy PDT rule in three days. `pdt_mode` cannot be assumed. | Build the `legacy_guard` accounting as specified; default `config/policy.live.yaml` to **M1 only** with M2 present but `allowed_modules` excluding it until the operator verifies the account's regime from `AccountInfo.pattern_day_trader`/`daytrade_count` and edits the policy. |
| D3 | §9 streaming, §12 daemon/watchdog, §13 integration tests | This build environment cannot open any connection to Alpaca. Streaming and trade-update code can only be verified here with fakes and recorded frames; real WebSocket behaviour (binary frames on paper, 406 on second connection, subscription cap) must be verified by the operator with the gated integration tests. | Write the gated tests first; treat every streaming component as unverified until the operator's run; keep the existing polling `Trader` path as the fallback runtime. |
| D4 | §3 Universe Tier 3 via `universe build` | Needs 60-day quotes/volume for all US common stocks; on the Basic plan that is thousands of REST calls at 200/min. | Implement `universe build` to scan a configurable candidate list (default: top-100 by market cap, static file) rather than all US common stocks; document it. |
| D5 | §11 cuts A–D from 2016 on 1-minute bars | SPY+QQQ 2016–2026 ≈ 5.2M minute bars (single-page-per-10k REST, ~520 requests: feasible). 15 Tier 3 names add ≈ 40M rows; DuckDB handles it but the fetch takes hours at Basic rate limits and must run on the operator's machine. | Phase 1 provides the fetch with resume; Phase 3 research for M3 runs on whatever the operator has fetched; the audit records coverage. |
| D6 | §4 daily-legacy reproduction "within 1e-6 relative" | The current engine's fills, ATR seeding and end-of-run liquidation are specific; the new engine must replicate them exactly through `fills=daily_legacy`. | Treat the current `engine.py` as the oracle; the reproduction test compares equity curves and trade lists from both engines on the cached proxy data. |
| D7 | Tests count and speed | 176 tests already take ~55 s; §13 adds ~55 more, some with simulated sessions. | Mark slow session simulations with `@pytest.mark.slow`, keep the default `python -m pytest -q` green and under ~3 minutes. |

Nothing in the spec conflicts with the eight non-negotiable rules; D2 is the only place where following the spec literally
(M1 and M2 in the live policy) would be unsafe for this account, and the change proposed keeps M2 out of live until verified.

## 4. Files expected to change per phase

- **Phase 1 (data):** new `bot/data/minute.py`, `bot/data/sessions.py`, `config/universe.yaml`; extend `bot/data/store.py`
  (tables `bars_1m`, `quotes`), `bot/data/quality.py`, `bot/data/providers.py` (`DataPlanError`), `bot/cli.py`
  (`data fetch --timeframe 1m`, `data check --timeframe 1m`, `universe build`); tests `tests/v15/test_minute_data.py`,
  `test_sessions.py`, `test_universe.py`; DATA.md.
- **Phase 2 (backtester):** new `bot/core/events.py`, `bot/core/intents.py`, `bot/backtest/fills.py`, `bot/backtest/engine_v15.py`,
  `bot/strategies/adapter.py`, `bot/features/engine.py`; extend `bot/execution/fake_broker.py` (SimBroker); tests
  `tests/v15/test_fills.py`, `test_engine_v15.py`, `test_legacy_reproduction.py`, `test_features.py`.
- **Phase 3 (research):** new `bot/strategies/v15/m1_vol_trend.py`, `m2_intraday_momentum.py`, `m3_residual_reversal.py`,
  `bot/research/protocol.py`, `research/PROTOCOL.md`, `research/RESULTS_V1_5.md`, `research/PROMOTIONS.md`, `research/UNSEAL_LOG.md`,
  `config/earnings.csv`; fix `bot/research/harness.py::classify_regimes` leakage; tests `tests/v15/test_protocol.py`, `test_modules.py`.
- **Phase 4 (spine):** new `bot/core/policy.py`, `bot/portfolio/allocator.py`, `bot/risk/policy_engine.py`, `bot/execution/oms.py`,
  `bot/execution/store.py`, `config/policy.paper.yaml`, `config/policy.live.yaml`; extend `bot/execution/broker.py` (limit, stop,
  OTO, bracket, replace, orders-since), `bot/risk/manager.py` (extend, not fork), `bot/execution/interlock.py` (policy fingerprint),
  `bot/cli.py` (`state migrate`, `risk unthrottle`, `gates`); tests `tests/v15/test_allocator.py`, `test_policy_engine.py`,
  `test_oms.py`, `test_store.py`, `test_broker_constraints.py`, extend `tests/test_architecture.py`.
- **Phase 5 (runtime):** new `bot/stream/marketdata.py`, `bot/stream/tradeupdates.py`, `bot/runtime/scheduler.py`,
  `bot/runtime/daemon.py`, `bot/runtime/watchdog.py`, `deploy/systemd/*.service`, `deploy/README.md`, `config/watchdog.yaml`;
  `bot/cli.py` (`run`, `watchdog`); tests `tests/v15/test_marketdata_hub.py`, `test_tradeupdates.py`, `test_scheduler.py`,
  `test_daemon.py`, `test_watchdog.py`; gated `tests/integration/test_v15_*.py`.
- **Phase 6 (paper start):** README.md, ARCHITECTURE.md, STRATEGY_SPEC.md, LIVE_RUNBOOK.md, `V1_5_FINAL_REPORT.md`; no logic.

## 5. Baseline reproduction rule

Every phase's final commit message states which of §2.1 (when the operator's cache is present) or §2.3 (always) numbers
were re-run and matched. `python -m bot backtest --strategy ma_crossover --symbol SP500 --start 2000-01-03 --end 2022-12-28`
must print total +95.72%, Sharpe 0.51, 26 trades, walk-forward +53.00%, Sharpe 0.55, 56 trades after every phase.
