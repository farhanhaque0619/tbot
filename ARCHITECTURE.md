# ARCHITECTURE — brain / reflex separation (Phase 9)

```
                 ┌──────────────────────────────────────────────────────────────────┐
                 │  LAYER A · RESEARCH / BRAIN            bot/research/, bot/advisor/ │
                 │  compare · surfaces · regimes · candidates · review · hypotheses   │
                 │  advisor (Jev/rule) in SHADOW MODE: consumes MarketState, logs     │
                 │  → may import: backtest, strategies, risk (read), data              │
                 │  → may NOT import: execution.broker, paper_loop, interlock, alpaca  │
                 └──────────────────────────────────────────────────────────────────┘
                                          │ proposals (markdown), never orders
                                          ▼  (operator reviews, tests, approves)
                 ┌──────────────────────────────────────────────────────────────────┐
                 │  LAYER B · LIVE EXECUTION / REFLEX      bot/execution/             │
                 │  Trader.run_cycle: bars → replay strategy → desired exposure →     │
                 │  MarketState → RiskManager.check_order → LiveInterlock (live)      │
                 │  → submit (deterministic client id) → sync fills → reconcile       │
                 │  → DecisionRecord (logs/decisions_*.jsonl) → Discord               │
                 │  small, typed, deterministic; unit-tested against FakeBroker        │
                 └──────────────────────────────────────────────────────────────────┘
                                          │
                 ┌──────────────────────────────────────────────────────────────────┐
                 │  RISK (below both)                      bot/risk/                  │
                 │  RiskLimits · SafeLiveLimits · RiskManager: update_equity,          │
                 │  can_open, position_qty, check_order → APPROVE / REJECT(code)      │
                 │  imports nothing from execution, research, advisor or strategies    │
                 └──────────────────────────────────────────────────────────────────┘
```

Enforced by `tests/test_architecture.py` (AST import scan):
- `bot.research.*` and `bot.advisor.*` never import a broker, the loop, the interlock, or `alpaca`.
- `bot.execution.*`, `bot.risk.*`, `bot.backtest.*` never import `bot.research` or `bot.advisor.jev`; no
  identifier in execution/risk mentions Kelly.
- `bot.risk.*` imports none of execution / research / advisor / strategies.
- The execution strategy registry contains only the two baselines; research candidates live in a separate registry.

## Data flow of one execution cycle (`bot/execution/paper_loop.py`)

1. `_sync_orders` — every non-terminal order in state is re-read from the broker by client id; fills (incl.
   partial) are booked, terminal-unfilled orders are handled, "submitting" orders unknown to the broker are marked
   lost and their session re-opened.
2. `get_account / get_positions / get_open_orders` — broker is authoritative; `_reconcile_positions` drops local
   records the broker no longer has, trusts broker quantities, adopts unknown positions (no stop, logged).
3. `RiskManager.update_equity` — daily halt / kill switch (percent limits and, in live safe mode, dollar limits).
   Kill switch → cancel all, close all, alert, refuse to trade until `risk reset`.
4. Per symbol not yet processed for the last completed session: load bars → replay strategy from scratch
   (risk-forced exits replayed) → desired exposure → protective stop check → `build_market_state` → (shadow advisor)
   → compare with broker position → `OrderIntent`.
5. `RiskManager.check_order` (≈ 25 named checks, first failure is the machine-readable code) → in live,
   `LiveInterlock.check` (10 gates + armed) → state written → `submit_market_order` → state written → alert.
6. `DecisionRecord` appended for every processed (symbol, session), including blocked/waiting/exception outcomes.

## What the advisor can and cannot do (Phase 10)

Can: receive a `MarketState` (all fields computed in code), return `{regime, setup_quality, direction, risk_state}`
from a closed vocabulary, be logged, be measured for calibration.
Cannot: see or compute equity/limits (it receives numbers, not authority), change risk parameters, approve or veto
an order, touch the broker, modify code. Shadow mode is the only mode; there is no code path from advice to orders.

## What the nightly review can and cannot do (Phase 15)

`python -m bot review` reads state and logs, writes `reports/review_*.md` with findings and PROPOSALS. It cannot
write to state, `.env`, code, or the broker. Promotion of any proposal goes through RESEARCH.md's process rule.


# V1.5 — the decision spine (Phases 2–5)

```
   market data (one WS)          trade updates (WS)            scheduler (close-relative marks)
   bot/stream/marketdata.py      bot/stream/tradeupdates.py    bot/runtime/scheduler.py
   dedup · order · 30m agg ·     host-verified · watermark ·   pre_open 09:00/09:29 · open · t1530 ·
   406 fatal · gap backfill      reconnect → reconcile         t1550 · t1558 · close · 16:30 · 19:05
            │ BarEvent/QuoteEvent          │ TradeUpdateEvent              │ ScheduleEvent
            └──────────────────────────────┴────────── EventBus ──────────┘
                                                   │
   ┌───────────────────────────────────────────────▼──────────────────────────────────────────────┐
   │  DAEMON  bot/runtime/daemon.py  (the same step the backtester runs: bot/portfolio/dispatch.py) │
   │  FeatureEngine.snapshot ──► modules (M1/M2/M3, own slice only) ──► TradeIntents              │
   │        ──► RiskEngine.admit ──► Allocator (caps scale DOWN only) ──► TargetPositions          │
   │        ──► OrderManager.reconcile: exits before entries · RiskManager.check_order on EVERY    │
   │            order incl. protective legs · store row committed BEFORE the broker call ·         │
   │            deterministic client id · OTO / GTC / DAY protection policy                        │
   │  trade updates ──► OrderManager.on_trade_update (idempotent) ──► ExposureLedger slices        │
   │  every 5 min ──► reconcile (broker is the authority) · every bar ──► kill switch on equity    │
   └───────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                   │ SQLite WAL  state/<env>.sqlite
   ┌───────────────────────────────────────────────▼──────────────────────────────────────────────┐
   │  WATCHDOG  bot/runtime/watchdog.py  (own process; reads the store and the broker)              │
   │  heartbeat · feed · trade updates · broker · positions · loss/drawdown · orphans · protection │
   │  alert → halt flag → cancel pending entries → flatten (only as config/watchdog.yaml allows)  │
   └──────────────────────────────────────────────────────────────────────────────────────────────┘

   RESEARCH (brain)  bot/research/protocol.py: cuts A/B/C, sealed D, trial registry, deflated Sharpe, stresses,
   causal regimes, pass/fail → research/RESULTS_V1_5.md → research/PROMOTIONS.md → RiskPolicy.load(require_promotions)
```

Layering, enforced by `tests/test_architecture.py`: `bot.core` and `bot.risk` import nothing above them;
`bot.strategies`, `bot.portfolio`, `bot.features`, `bot.advisor` cannot reach order submission; `bot.stream` cannot
import `bot.execution`; the watchdog cannot import strategies, the allocator, the OMS or features; nothing in `bot/`
mutates a `RiskPolicy` (frozen, fingerprinted; the arm flow and the daemon compare fingerprints); the live policy is
stricter than the paper policy on every limit that matters.

Two execution paths coexist and never share a process: V1 (`python -m bot trade`, daily bars, the two baselines,
JSON state) and V1.5 (`python -m bot run`, minute bars, modules, SQLite). `python -m bot state migrate` moves the V1
state into the store once.
