# V1_5_CODE_PROMPT.md

You are Claude Code working inside the `tbot` repository (`farhanhaque0619/tbot`, currently at or after commit `2cb338a`). Your job is to transform this daily-bar trading system into **V1.5: a multi-strategy, minute-bar, autonomous trading platform on Alpaca**, following the specification below exactly. The research behind the specification is in `V1_5_RESEARCH_REPORT.md` at the repo root if it has been added; this prompt is self-contained and takes precedence where they differ.

Read all of this before touching anything.

## 0. Non-negotiable rules

1. **Never place a live order.** Do not read, print, or use `ALPACA_LIVE_*` credentials. Every integration test, smoke test and runtime you write defaults to paper and refuses live unless the existing interlock (`bot/execution/interlock.py`) passes, and you must not weaken that interlock.
2. **Do not "make it profitable."** Your success criteria are engineering criteria (sections 12-15) and research-protocol criteria (section 11). A module that fails the protocol stays unpromoted. You never tune a parameter against the holdout, and you never report a Sharpe without its trial count and deflated value.
3. **Risk sits below strategy.** No strategy, allocator, advisor, watchdog or CLI flag may raise any limit in `RiskPolicy`. The only automatic change permitted is tightening (throttling). Extend the architecture import-scan tests in `tests/test_architecture.py` to enforce this.
4. **Preserve the baselines.** `ma_crossover` and `mean_reversion` must keep running, through the new risk and execution spine, via an adapter. `python -m bot backtest --strategy ma_crossover ...` must keep producing the same numbers on daily bars (within floating-point rounding) after every phase.
5. **Preserve every existing safety property**: separate paper/live keys with prefix and host checks, `TRADING_ENV`, `--live`, arm/disarm with TTL and fingerprint, `LIVE_AUTONOMOUS_TRADING=false` default, `SAFE_LIVE_TEST_MODE` caps, deterministic client order ids, state written before and after submission, broker-as-authority reconciliation, log redaction, research-cannot-order, advisor-shadow-only.
6. **Deterministic, not AI.** Regime state, signals, sizing and stops are arithmetic. The advisor stays in shadow mode with no path to orders.
7. **No fabricated Alpaca features.** Only use what the current docs document. Where behaviour is uncertain, write the code to detect and handle both cases and add a gated integration test.
8. **Commit at every checkpoint** (section 18) with the tests green. Never leave the repo in a state where `python -m pytest -q` fails.

## 1. Phase 0: repository-first audit (no code changes)

Before writing code:
- Read every file under `bot/`, `tests/`, `research/`, `scripts/`, and every `*.md` at the root. Run `python -m pytest -q` and record the count.
- Run, with paper credentials if available: `python -m bot doctor --paper`, `python -m bot data fetch --symbol SPY --symbol QQQ --start 2016-01-01`, `python -m bot data check`, `python -m bot backtest --strategy ma_crossover --symbol SPY --start 2017-01-01`, the same for `mean_reversion`, and `python -m bot research compare --symbol SPY --start 2017-01-01`. Record every number in `V1_5_AUDIT.md`. These are the frozen baselines that every later phase must reproduce.
- Confirm from the code (not from any document) the answers to: what order types the broker layer supports; how state is persisted; how a restart during submission is handled; how the ATR stop is applied in backtest and in execution; where regime labels come from; whether anything in `bot/research` or `bot/advisor` can reach `broker.submit_*`.
- Write `V1_5_AUDIT.md`: what exists, what the tests cover, the frozen numbers, and a list of every file you expect to change in each phase.
- Commit: `v1.5-phase0-audit`.

## 2. Target architecture

```
Alpaca WS (IEX 1m bars, quotes)      Alpaca WS trade_updates
        |                                     |
  MarketDataHub  --BarEvent-->  FeatureEngine --FeatureSnapshot-->  Strategies (M1, M2, M3, adapters for ma_crossover, mean_reversion)
        |                                                                  |  list[TradeIntent]
   SIP lagged backfill                                               Allocator (vol-normalise, caps, correlation haircut)
                                                                           |  list[TargetPosition]
                                                                     RiskEngine (RiskPolicy caps + exposure ledger + existing check_order)
                                                                           |  approved OrderIntents
                                                                     OrderManager (state machine on trade_updates, OTO/bracket, replace, cancel)
                                                                           |
                                                                     StateStore (SQLite WAL)      Watchdog (separate process)      Reporter/Alerts
```

Package layout to create (keep existing packages; add these):

```
bot/core/events.py          BarEvent, QuoteEvent, TradeUpdateEvent, ScheduleEvent (frozen dataclasses)
bot/core/intents.py         TradeIntent, TargetPosition
bot/core/policy.py          RiskPolicy (pydantic, frozen, fingerprint())
bot/data/minute.py          minute-bar provider (SIP lagged + IEX), aggregation to 5/15/30m on ET boundaries
bot/data/sessions.py        session calendar with early closes; ET-aware scheduler helpers
bot/stream/marketdata.py    MarketDataHub (single WS connection, reconnect, dedupe, gap backfill)
bot/stream/tradeupdates.py  TradeUpdatesClient (WS + polling fallback with watermark)
bot/features/engine.py      FeatureEngine, FeatureSnapshot; incremental features
bot/strategies/v15/m1_vol_trend.py
bot/strategies/v15/m2_intraday_momentum.py
bot/strategies/v15/m3_residual_reversal.py
bot/strategies/adapter.py   LegacyStrategyAdapter: Strategy.on_bar -> TradeIntent
bot/portfolio/allocator.py  Allocator
bot/risk/policy_engine.py   RiskEngine wrapping RiskManager + ExposureLedger + PDT accounting
bot/execution/oms.py        OrderManager, OrderRecord state machine
bot/execution/broker.py     extend Broker protocol: submit_limit, submit_stop, submit_oto, submit_bracket, replace_order, cancel_order (by id), get_orders_since(watermark)
bot/execution/store.py      SQLite StateStore (WAL) + migration from state/*.json
bot/runtime/daemon.py       Daemon (boot, reconcile, subscribe, schedule, cycle, report)
bot/runtime/scheduler.py    ET-aware schedule of events (pre-open, each 30m bar close, 15:30, 15:50, 15:58, close, 16:30, 19:30)
bot/runtime/watchdog.py     Watchdog (separate entrypoint `python -m bot watchdog`)
bot/backtest/engine_v15.py  minute-bar, multi-module, auction-aware engine
bot/backtest/fills.py       fill models (marketable limit, market, OPG, CLS, stop election, partials)
bot/research/protocol.py    dataset cuts, trial registry, deflated Sharpe, bootstrap, cost stress, attribution
deploy/systemd/tbot.service, deploy/systemd/tbot-watchdog.service, deploy/README.md
config/policy.paper.yaml, config/policy.live.yaml, config/universe.yaml, config/earnings.csv
```

## 3. Data architecture (Phase 1)

- Extend `BarStore` (DuckDB) with tables `bars_1m(symbol, ts_utc, o, h, l, c, v, trade_count, vwap, feed)` and `quotes(symbol, ts_utc, bid, ask, bid_size, ask_size, feed)`. Keep `bars_1d`.
- `MinuteProvider.fetch(symbol, start, end, feed)` using `StockBarsRequest(TimeFrame.Minute, ...)`. On the Basic plan SIP data newer than 15 minutes is refused; implement `end = min(end, now - 16 min)` for SIP and no lag for IEX. Detect the "subscription does not permit" error and surface it as `DataPlanError`, never silently fall back for a query that needs recency.
- Aggregation: `aggregate(bars_1m, minutes=30)` on exact ET boundaries starting 09:30, session-aware (an early close at 13:00 yields a final partial bar flagged `partial=True`, which strategies must treat as complete only if `is_session_end`).
- Quality: extend `bot/data/quality.py` for minute data: missing minutes per session, duplicate timestamps, OHLC consistency, zero-volume runs, bars outside the session, split discontinuities. `python -m bot data check --timeframe 1m`.
- Session calendar: `bot/data/sessions.py` builds sessions from `/v2/calendar` (early closes included) with a cached fallback; provides `session_bounds(date)`, `is_regular_hours(ts)`, `cutoffs(date)` for OPG (09:28) and CLS (15:50, or 12:50 on an early close).
- Universe: `config/universe.yaml` with tiers exactly as follows. Tier 1: SPY, QQQ, IWM, DIA. Tier 2: XLK, XLF, XLE, XLV, XLY, XLP, XLI, XLU, XLB, XLRE, XLC. Tier 3: 15 stocks chosen by `python -m bot universe build` from the 60-day median dollar volume of US common stocks with price ≥ $20, `fractionable`, `tradable`, `easy_to_borrow`, 60-day median quoted spread ≤ 5 bps; the command writes the list with a date stamp and the operator commits it. Total must be ≤ 30 (Basic-plan WebSocket cap); the hub refuses to start with more than 30 symbols unless `DATA_PLAN=plus` is set.

Tests (Phase 1): SIP lag boundary; IEX no lag; DataPlanError surfaced; aggregation boundaries; early-close partial bar; holiday; missing minute detection; duplicate minute; DST transition day; universe cap enforcement.

## 4. Backtester (Phase 2)

`bot/backtest/engine_v15.py` must:
- Iterate 1-minute bars across all symbols on a shared session clock; emit 30-minute `BarEvent`s and `ScheduleEvent`s (pre-open, 15:30 window, 15:50 cutoff, 15:58, close) identical to the live scheduler.
- Run the same `FeatureEngine`, strategies, `Allocator`, `RiskEngine`, and an `OrderManager` bound to a `SimBroker` (extend `FakeBroker`) so the decision path is byte-identical between backtest and live.
- Fill models in `bot/backtest/fills.py` with explicit parameters, all defaulting to conservative values:
  - `marketable_limit`: fills at the next 1-minute bar's open plus half-spread plus `slippage_bps`; spread from the quote table when present, else from a per-symbol schedule in config (ETFs 1 bp, Tier 3 3 bps by default).
  - `market`: same as marketable limit plus `market_extra_bps` (default 1).
  - `opg`: fills at the session's official open plus `open_auction_slippage_bps` (default 3).
  - `cls`: fills at the session's official close plus `close_auction_slippage_bps` (default 1).
  - `stop`: elects on the first 1-minute bar whose low ≤ stop (for a sell stop) and fills at the next bar's open minus `stop_slippage_bps` (default 5); across a session boundary, fills at the next open (gap risk realised).
  - Partial fills: if order notional exceeds `participation_cap` (default 1%) of the bar's SIP dollar volume, fill only that fraction per bar and carry the remainder to the next bar; cancel unfilled at the order's TIF end.
- Order priority within an event: risk exits, then module exits, then entries.
- No-lookahead: a fill may use only bars strictly after the decision timestamp. Add a mutation test that changes future bars and asserts identical decisions.
- Reproduction: when fed daily bars through the legacy adapter with `fills=daily_legacy`, it must reproduce `engine.py`'s frozen Phase 0 numbers within 1e-6 relative.
- Cost stress hooks: multipliers for spread and slippage, `execution_delay_bars`, `drop_signal_fraction`, `random_adverse_fill(prob, bps)`, all seeded.
- Output: `BacktestResultV15` with per-module equity, per-module trade lists, fills, costs, exposure series, and overnight/intraday P&L attribution.

Tests (Phase 2): auction fills, stop election across a gap, partial fills, order priority, multi-module netting on one symbol, daily-legacy reproduction, future-mutation invariance, cost-stress determinism under a seed.

## 5. Strategy interface, features, and the three modules (Phases 2-3)

### 5.1 TradeIntent

```python
@dataclass(frozen=True)
class TradeIntent:
    symbol: str
    module_id: str                 # "M1" | "M2" | "M3" | "ma_crossover" | "mean_reversion"
    ts: datetime                   # decision time, tz-aware ET
    direction: int                 # +1 long, -1 short, 0 flat (close module slice)
    horizon_seconds: int
    reference_price: float
    volatility: float              # sigma over the horizon, as a fraction of price
    risk_budget_pct: float         # per-trade risk in fraction of equity (module default; allocator may scale DOWN only)
    signal_strength: float         # [0, 1]
    expected_edge_bps: float | None
    invalidation_price: float | None
    max_holding_seconds: int
    entry_style: str               # "marketable_limit" | "opg" | "cls" | "market"
    exit_style: str                # "cls" | "market_1558" | "marketable_limit" | "opg"
    overnight_ok: bool
    protective_stop_price: float | None   # catastrophe stop the OMS must place broker-side when possible
    tag: str = ""
```

Strategies implement `on_event(event, snapshot: FeatureSnapshot, positions: PositionView) -> list[TradeIntent]`. They never see equity, cash or other modules' positions beyond their own slice. `LegacyStrategyAdapter` wraps `Strategy.on_bar` and emits an intent with `direction=target`, `horizon_seconds=86400`, `volatility=ATR14/price`, `risk_budget_pct=policy.legacy_risk_pct`, `entry_style="opg"`, `overnight_ok=True`, and `protective_stop_price` from the legacy ATR rule only when `policy.legacy_atr_stop=True`.

### 5.2 FeatureEngine (incremental, O(1) per bar per feature)

Per symbol: realised volatility of 1-minute, 30-minute and daily returns over 21 and 63 periods (annualised); `r1` = ln(close of the 09:30-10:00 30-minute bar / previous session close); `r12` = ln(close of 15:00-15:30 / close of 14:30-15:00); `sigma_last30` = 21-session standard deviation of the 15:30-16:00 return; session VWAP; `beta_60d` vs SPY and `res5` = 5-session return minus beta × SPY 5-session return; `sigma_res_21d`; latest spread in bps from the quote table (lagged SIP) with a freshness flag; `relvol_1515` = SIP volume 09:30-15:15 / 21-session median (available only from lagged SIP; `None` otherwise); 12-month-minus-1-month return; earnings-within-5-sessions flag from `config/earnings.csv`; synthetic-bar fraction over the last 30 minutes.

### 5.3 M1: vol-managed index trend (SPY, QQQ)

- On the daily close event: `sigma = max(vol_21d, vol_63d)`; `tsmom = ret_12m_ex_1m > 0`; `w = clip(0.10 / sigma, 0, 0.60) * tsmom`.
- Emit an intent (`direction=+1`, `risk_budget_pct` encoded as `target_weight=w` via `signal_strength=w/0.60` and `volatility=sigma`; the allocator has an M1-specific path that treats the intent as a target weight) on Mondays, or on any day where `|w - w_actual| > 0.10`. `entry_style="cls"` for whole-share accounts, `"market_1555"` for fractional; `overnight_ok=True`; `protective_stop_price = entry * (1 - 4 * sigma_weekly)` where `sigma_weekly = sigma / sqrt(52)`. No ATR stop.
- Exit: `w=0` when `tsmom` is false.

### 5.4 M2: market intraday momentum (SPY, QQQ)

- On the 15:30 ScheduleEvent: `s = sign(r1) if |r1| > k * sigma_last30 else 0`, `k=0.5` default, surface over {0.25, 0.5, 0.75, 1.0}. Variants to evaluate in Phase 3 (not to choose here): agreement filter `sign(r1) == sign(r12)`; `relvol_1515 > 1.0` filter; entry time surface {15:15, 15:30, 15:45}.
- Intent: `direction=s`, `horizon_seconds=1800`, `volatility=sigma_last30`, `risk_budget_pct=0.0025`, `entry_style="marketable_limit"`, `exit_style="cls"` (whole shares) or `"market_1558"` (fractional), `overnight_ok=False`, `max_holding_seconds=1800`, `protective_stop_price=None` (software fat-tail guard at 3 × sigma_last30 handled by the module on 1-minute bars).
- In live, `RiskPolicy.allow_short=False` turns `s=-1` into no trade; the module must not know or care.
- Entry execution rule (OMS): limit at ask + 1 tick for a buy; if unfilled after 20 s, cancel and re-price once at ask + 2 ticks; abandon at 15:33.
- Live gating: M2 admission requires `policy.pdt_mode == "intraday_margin"` or, under `"legacy_guard"`, that the account's `daytrade_count` allows the trade without creating a 4th day trade in 5 sessions; the RiskEngine computes this from `AccountInfo.daytrade_count` and its own ledger.

### 5.5 M3: large-cap residual reversal (Tier 3, PAPER-ONLY)

- On the daily close event: rank `res5 / sigma_res_21d` across eligible Tier 3 names (spread ≤ 5 bps, no earnings within 5 sessions, synthetic-bar fraction < 0.2). Candidates: bottom 3.
- Intent: `direction=+1`, `horizon_seconds=5*86400`, `volatility=sigma_res_21d*sqrt(5)`, `risk_budget_pct=0.0025`, `entry_style="opg"` (whole) or `"limit_at_prev_close_cancel_0945"` (fractional), `exit_style="cls"`, `overnight_ok=True`, `max_holding_seconds=5 sessions`, `invalidation_price=None`, `protective_stop_price = entry * (1 - 3 * sigma_res_21d * sqrt(5))`.
- Exit: `res5 > 0` on any close, or 5 sessions elapsed, or close below the fat-tail level.
- `RiskPolicy.allowed_modules` for live must not include M3 until the protocol's promotion record exists (section 11); the RiskEngine refuses M3 intents in live otherwise.

## 6. Allocator (Phase 4)

`Allocator.allocate(intents, positions, equity, policy, corr_matrix) -> list[TargetPosition]`:
1. For M2/M3/legacy intents: `notional = risk_budget_pct * equity / volatility`. For M1: `notional = target_weight * equity`.
2. Apply, in order: per-symbol cap (`policy.max_symbol_exposure_pct * equity`), per-module gross cap, sector cap (30% of gross, sectors from `config/universe.yaml`), portfolio gross cap, net bounds. When a cap binds, scale only the new intents pro rata, never existing positions.
3. Correlation haircut: if the mean pairwise 21-day correlation of residual returns among intended positions > 0.5, scale new notional by 0.7.
4. Netting: intents on the same symbol from different modules net to one target; the ledger records per-module slices.
5. Rounding: whole shares when `abs(qty) >= 1` and the account is whole-share-capable for the entry style; else fractional to 3 decimals; a target under $1 notional is dropped.

No optimiser, no Kelly. Log every scaling decision.

## 7. Risk integration (Phase 4)

- `RiskPolicy` (pydantic frozen) fields: `allowed_modules, allowed_symbols, max_order_notional_pct, max_symbol_exposure_pct, max_module_gross_pct (dict), max_gross_pct, max_net_pct, min_net_pct, max_daily_loss_pct, max_drawdown_pct, max_open_positions, allow_short, allow_margin, allow_overnight (dict per module), allow_extended_hours, max_spread_bps (dict ETF/stock), max_stale_seconds_intraday, max_stale_seconds_auction, pdt_mode, require_broker_protection_overnight, legacy_atr_stop, legacy_risk_pct`. `fingerprint()` is a SHA-256 of the canonical JSON. The arm flow displays and stores the fingerprint; the daemon refuses to run if the loaded policy's fingerprint differs from the armed one.
- Defaults: `config/policy.live.yaml` (modules M1 and M2, gross 100%, net 0-100%, short off, margin off, overnight M1 only, daily loss 2%, drawdown 12%, 6 positions, `pdt_mode` from the account) and `config/policy.paper.yaml` (M1, M2, M3, gross 150%, net −50 to 150%, short on, margin on, 12 positions). Safe-mode dollar caps from `SafeLiveLimits` continue to apply on top in live.
- `RiskEngine`: wraps the existing `RiskManager` (do not fork its logic; extend it). Adds an `ExposureLedger` (gross, net, per-module, per-symbol, per-sector, computed from broker positions plus in-flight orders), PDT accounting, the broker-protection requirement, and intent admission (`admit(intent) -> Decision` before allocation). `check_order` remains the final gate on every `OrderIntent`, including every protective leg.
- Exits are never blocked by entry limits (existing behaviour); protective legs are treated as exits.
- Throttle: if a module's last 60 trades have a negative running net mean, halve its `risk_budget_pct` and alert; only the operator restores it (`python -m bot risk unthrottle --module M2`). This is the only automatic policy change and it only tightens.

## 8. Alpaca integration and broker-side protection (Phase 4)

- Extend `Broker` with `submit_limit_order(symbol, qty, side, limit_price, cid, tif, extended_hours=False)`, `submit_stop_order(...)`, `submit_oto(entry: OrderSpec, stop_loss: StopSpec)`, `submit_bracket(entry, take_profit, stop_loss)`, `replace_order(order_id, **fields)`, `cancel_order(order_id)`, `get_orders_since(after: datetime, status="all")`, `get_order_by_client_id` (exists).
- Enforce documented constraints in code before calling the API (and add tests): fractional orders are DAY only and cannot be OPG/CLS/GTC or part of a bracket/OTO; extended-hours orders must be limit DAY/GTC; OPG must be submitted before 09:28 or after 19:00; CLS before 15:50 or after 19:00; bracket stop must be ≥ $0.01 from the base price; limit and stop prices rounded to the tick (2 decimals ≥ $1); notional orders cannot be replaced.
- Protective stops policy in the OMS:
  - Whole-share overnight position: place an OTO (entry + stop-loss) when the entry is placed; if the entry was OPG/CLS (no OTO support with auction TIFs in the tables, verify in an integration test), place a standalone GTC stop immediately on fill.
  - Fractional overnight position: place a DAY stop order at fill, and re-place it at 09:29 every session (`ScheduleEvent.pre_open`); mark the position `unprotected_overnight=True` in the ledger and report it nightly.
  - M2 positions: no broker-side stop; software guard.
  - If a protective order is rejected, mark `unprotected=True`, alert, and if the policy requires protection for that position, flatten at the next eligible time.
- Client order ids: `<run_id>-<module>-<symbol>-<session>-<seq>-<kind>` with `kind ∈ {entry, exit, stop, tp}`; `seq` increments per (module, symbol, session).

## 9. Streaming (Phase 5)

- `MarketDataHub`: one connection to `wss://stream.data.alpaca.markets/v2/{iex|sip}` (feed from `DATA_PLAN`); authenticate; subscribe to `bars` for all symbols and `quotes` for symbols with a position or open order (subscription list updated on position change); heartbeat timer; reconnect with exponential backoff and jitter capped at 300 s; refuse to open a second connection; handle the 406 error frame explicitly. Deduplicate by (symbol, ts); drop out-of-order bars with a log; on reconnect, backfill the gap from REST (IEX for the gap, SIP lagged when older than 16 minutes) and mark backfilled bars. Publish `BarEvent` on the internal bus; the 30-minute aggregator subscribes.
- `TradeUpdatesClient`: `wss://paper-api.alpaca.markets/stream` or `wss://api.alpaca.markets/stream` (chosen by env with the same host verification as the REST client), `listen` to `trade_updates`, binary frames on paper per the docs; every event goes to the OMS state machine; on reconnect, poll `get_orders_since(watermark)` and reconcile; duplicates by (order_id, event, timestamp) are idempotent.
- Freshness: the RiskEngine reads `hub.last_bar_age(symbol)` and `hub.connected`; intraday intents are refused after 90 s without a bar in regular hours.

## 10. Persistence and reconciliation (Phases 4-5)

- `bot/execution/store.py`: SQLite in WAL mode, tables `orders, fills, positions (per module slice), risk_state, decisions, heartbeats, throttles, protective_orders, watermarks`. Every submit is a transaction: insert `submitting` row, commit, call broker, update row, commit. Provide `migrate_from_json(path)` and a `python -m bot state migrate` command; keep the JSON reader for one release.
- Reconciliation on boot and every 5 minutes: broker positions vs ledger (adopt unknown positions as `module_id="orphan"` with a protective stop per policy, or flatten if the policy forbids them); broker open orders vs ledger (cancel unknown non-protective orders; adopt unknown protective orders); orders since watermark vs fills table. Any discrepancy is a `reconciliation` decision record and an alert at warning level; two consecutive unresolved discrepancies halt entries.

## 11. Research protocol (Phase 3)

Implement `bot/research/protocol.py` and `research/PROTOCOL.md`:
- Fixed cuts: A 2016-01..2018-12 (exploration), B 2019-01..2021-12 (development), C 2022-01..2023-12 (validation), D 2024-01..2026-06 (holdout, sealed). `python -m bot research run --module M2 --cut B` refuses `--cut D` unless `--unseal --reason "<text>"` is given, and logs the unsealing with code and config hashes to `research/UNSEAL_LOG.md`.
- Trial registry: every backtest run through the protocol appends a row (module, params hash, cut, metrics) to `research/trials.sqlite`; reports compute the deflated Sharpe from the count of trials on the same module and cut.
- Required report per module (`research/RESULTS_V1_5.md`, generated): parameter surface on B (median, best, neighbour ratio); validation on C with block-bootstrap 90% interval of net expectancy; cost stress on C (2x spread, 2x slippage, 1-bar delay, 10% dropped signals, random adverse 2 bps on 20%); regime table on C with causal regime labels (trend via 126-day return sign, vol via trailing 63-day quantile computed only from past data); overnight/intraday attribution; deflated Sharpe; and a plain-English pass/fail against these criteria: surface median Sharpe > 0 and ≥ 70% of neighbours within 30% of the best; C bootstrap lower bound > 0; every stress leaves the point estimate positive and within 60% of base; no regime with Sharpe < −0.5 covering > 25% of the sample; attribution consistent with the module's mechanism (M2 intraday, M3 overnight).
- Promotion record: `research/PROMOTIONS.md` gets an entry only when a module passes B and C and, once, D. `RiskPolicy` loading checks this file before allowing a module in live.
- Fix the harness's regime-label leakage and replace hand-chosen walk-forward grids with declared surfaces; keep `compare` and `surface` working for the baselines.

## 12. Autonomous runtime, watchdog, deployment, monitoring (Phase 5)

- `python -m bot run --env paper --policy config/policy.paper.yaml` starts the daemon; `--env live` additionally requires the full interlock and `LIVE_AUTONOMOUS_TRADING=true`, unchanged.
- Daemon lifecycle: boot (config, policy fingerprint check, env verification, store open, heartbeat) → reconcile → subscribe → scheduler → cycles → graceful shutdown on SIGTERM (finish the current cycle, cancel non-protective open entries, persist, exit 0). Crash: exit non-zero; systemd restarts with `RestartSec=10`, `StartLimitBurst=5`.
- Watchdog (`python -m bot watchdog`, its own systemd unit): every 30 s checks process heartbeat age, market-feed freshness, trade-updates connection, broker reachability, ledger-vs-broker position agreement, daily P&L vs policy, orphaned orders, unprotected overnight positions. Actions, in escalation order and only as configured in `config/watchdog.yaml`: alert; halt entries (writes a `halt` flag the daemon honours); cancel pending entries; flatten per policy (`flatten_on: [heartbeat_stale_300s, drawdown_breach]`); never touches protective legs except when flattening. The watchdog has read-only broker access plus cancel/close permissions and imports nothing from `bot.strategies` (architecture test).
- Deployment: `deploy/README.md` describing a small Ubuntu 24 VPS in a US-East region, chrony, a non-root `tbot` user, `EnvironmentFile=/etc/tbot/env` (root:tbot 640), `git` checkout of a tagged release, `python3.11 -m venv`, both systemd units enabled, journald plus rotated `logs/*.jsonl`, nightly snapshot. Explain why not a laptop (sleep, updates, network) and why Docker is optional.
- Alerts (Discord, existing `Alerter`): startup (config, policy fingerprint, universe, modules); entry (symbol, module, size, reference, reason, protective level); exit (P&L, reason); fill with slippage; partial fill; reconnect (warning); reconciliation discrepancy (warning); throttle (warning); risk limit or kill (critical); watchdog action (critical). No per-cycle chatter; a daily summary at 16:30 ET.
- Reports: `python -m bot review` extended with per-module expectancy, slippage vs assumption, attribution, throttle status, unprotected positions.

## 13. Required tests (all must exist and pass; put them under `tests/v15/`)

Market data reconnect · trade-update reconnect · duplicate bar events · duplicate trade-update events · duplicate order submission after restart · partial fill then fill · partial fill then cancel · out-of-order bars · out-of-order trade updates · stale quote refuses intraday sizing · stale bar halts entries · missing bars flagged and threshold enforced · market holiday (no events) · early close (compressed schedule, CLS cutoff 12:50) · opening auction fill model · closing auction fill model · position mismatch adopts/flattens per policy · orphaned order cancelled · restart with open position re-attaches protection · restart with pending order resolves without duplicate · network loss then recovery · broker 5xx retry then halt · broker 429 backoff · kill switch cancels non-protective orders and flattens · daily loss halt blocks entries but not exits · max gross exposure binds and scales new intents · symbol concentration cap · module concentration cap · sector cap · fractional rounding and $1 minimum · short refused when policy forbids or asset not shortable · spread too wide refuses entry · volatility spike (fat-tail guard) exits · protective stop placed, confirmed, and re-placed daily for fractional · bracket/OTO rejection falls back and marks unprotected · PDT legacy guard blocks the 4th day trade · policy fingerprint mismatch refuses to run · graceful shutdown persists and exits 0 · SIGKILL mid-submission then restart · legacy adapter reproduces frozen baseline numbers · architecture: strategies/allocator/advisor/watchdog cannot import broker submit paths or mutate policy · research: sealed holdout refuses without unseal, trial registry counts, deflated Sharpe computed.

Integration tests (gated by `RUN_ALPACA_INTEGRATION=1`, paper only): minute-bar fetch with SIP lag error and IEX success; WS subscribe/receive/reconnect against the paper stream; submit limit, stop, OTO, bracket on a $1-notional-equivalent whole share where possible, read back nested legs, cancel; verify OPG/CLS acceptance windows; verify fractional DAY-only rejections; verify trade_updates binary frames on paper.

## 14. Paper gates and live gates (encode as `python -m bot gates`)

Paper → live candidate: ≥ 60 sessions autonomous; zero unreconciled discrepancies, orphans, duplicates; realised slippage within 3 bps of the backtest assumption; each watchdog action exercised at least once; M2 ≥ 120 paper trades with net expectancy of the same sign as the backtest; promotion record exists for every module in the live policy.
Live step-ups: 20 sessions at safe-mode caps, then ≤ 2x per month with a fresh review; a 5 bps slippage degradation vs paper halts entries.

## 15. Backwards compatibility and documentation

- Keep `python -m bot backtest`, `research compare`, `research surface`, `paper --once`, `smoke`, `doctor`, `status`, `live arm/disarm/check`, `risk reset` working.
- Update README.md, ARCHITECTURE.md (new diagram), DATA.md (minute bars, feeds, lag), STRATEGY_SPEC.md (M1, M2, M3 exact rules and parameters), LIVE_RUNBOOK.md (policy fingerprint, PDT check, protective-order policy, watchdog), and add `research/PROTOCOL.md`, `research/RESULTS_V1_5.md`, `deploy/README.md`, `V1_5_AUDIT.md`, `V1_5_FINAL_REPORT.md` (same 20-question structure as FINAL_REPORT.md, plus "what could I be wrong about").

## 16. What you must not do

Do not add indicators without a stated hypothesis and a place in the protocol. Do not add ML. Do not let the advisor influence intents. Do not reoptimise daily. Do not use the holdout. Do not enable shorts or margin in the live policy. Do not change `SafeLiveLimits` defaults. Do not store or print secrets. Do not delete the two baselines or the daily engine until the new engine reproduces them and the operator has signed off in a commit message.

## 17. Definition of done for V1.5

1. All Phase 0 baseline numbers reproduce through the new spine.
2. All tests in section 13 pass offline; gated integration tests pass against paper.
3. `python -m bot run --env paper` runs a full session autonomously, survives a forced restart mid-session with no duplicate orders and correct reconciliation, and produces the daily report.
4. `research/RESULTS_V1_5.md` exists with pass/fail for M1, M2, M3 on cuts B and C, with trial counts and deflated Sharpes, and the holdout still sealed.
5. The watchdog runs as a separate process and has demonstrably halted and flattened in a test session.
6. Documentation updated; `V1_5_FINAL_REPORT.md` written.

## 18. Commit and checkpoint plan

Tag each: `v1.5-phase0-audit`, `v1.5-phase1-data`, `v1.5-phase2-backtester`, `v1.5-phase3-research`, `v1.5-phase4-spine`, `v1.5-phase5-runtime`, `v1.5-phase6-paper-start`. Within each phase commit at least after: interfaces defined, tests written (red), implementation (green), docs updated. Every commit message states which frozen baseline numbers were re-verified. If any phase cannot be completed as specified because an Alpaca behaviour differs from the docs, stop, write the discrepancy into `V1_5_AUDIT.md` with the exact API response, and propose the smallest safe change before proceeding.
