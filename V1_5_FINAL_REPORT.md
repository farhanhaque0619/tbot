# V1_5_FINAL_REPORT — state of the system after V1.5 Phases 0–6 (2026-09-26)

Same structure as FINAL_REPORT.md (20 questions), plus "what could I be wrong about". Everything below was built and
tested in an environment with **no network route to Alpaca**; the V1 paper integration was verified earlier by the
operator (doctor OK, integration suite, closed-market smoke test). No V1.5 component has yet run against the real
paper API. Nothing here claims an edge.

## 1. Repository audit
V1_5_AUDIT.md: frozen baselines (§2), the seven pre-build discrepancies D1–D7 (§3), files per phase (§4), the baseline
rule (§5), and one record per phase with decisions D8–D28 (§6–§9). Tags: `v1.5-phase0-audit` … `v1.5-phase6-paper-start`
(created locally; pushing tags from this environment is refused by the git proxy, the operator pushes them).

## 2. Exact current trading strategies in plain English
V1 baselines unchanged (`ma_crossover`, `mean_reversion`; FINAL_REPORT.md §2). V1.5 modules (STRATEGY_SPEC.md, V1.5
section): **M1** on each daily close sets a target weight in SPY/QQQ of min(0.10 / realised vol, 0.60) when the
12-month-minus-1-month return is positive, else zero; rebalances on Mondays or when the weight drifts by more than
0.10. **M2** at 15:30 goes with the sign of the first-30-minute return when it exceeds 0.5 × the 21-session standard
deviation of the last-30-minute return, and is flat by the close (CLS at 15:50 for whole shares, market at 15:58 for
fractional), with a 3-sigma software fat-tail exit. **M3** on each daily close buys the three most negative residual
(vs SPY beta) 5-session returns among eligible Tier 3 names and exits when the residual turns positive, after 5
sessions, or below a 3-sigma fat-tail level. All three are research candidates; none is promoted.

## 3. Exact parameters
Policies (`config/policy.paper.yaml`, `config/policy.live.yaml`; fingerprinted SHA-256 of canonical JSON): paper allows
M1/M2/M3 + baselines, gross 150%, net −50…150%, shorts and margin on, 12 positions; live allows **M1 only** on SPY/QQQ,
gross 100%, net 0…100%, no shorts, no margin, daily loss 2%, drawdown 12%, 6 positions, `pdt_mode=legacy_guard`,
protection required overnight, `orphan_policy=adopt`. Module defaults: M1 target_vol 0.10 / cap 0.60 / band 0.10 /
stop 4 σ-weeks; M2 k 0.5 / entry 15:30 / risk 0.25% / fat tail 3σ; M3 n 3 / hold 5 / spread ≤ 5 bps / synthetic < 0.2 /
risk 0.25%. Declared research surfaces in research/PROTOCOL.md. Safe-mode dollar caps (V1) still apply in live.

## 4. Exact trades it is capable of making
Through the OMS: market, marketable limit (ask + 1 tick, one re-price to ask + 2 ticks, abandoned at 15:33), OPG and
CLS auction orders (whole shares only), limit-at-previous-close cancelled 09:45 (fractional M3), stop orders as
protection (OTO leg on whole-share market/limit entries, standalone GTC after auction fills, DAY re-placed at 09:29 for
fractional), flatten at market on kill switch / lost protection / watchdog. Client ids
`<run>-<module>-<symbol>-<session>-<seq>-<kind>`. Everything passes `RiskManager.check_order`.

## 5. Can it short? Paper policy yes (M2 emits −1); live policy no (`allow_short=false` turns it into no trade at admission).
## 6. Can it use margin? Paper policy yes (gross 150%); live policy no (gross 100%, cash-capped).
## 7. Can it trade fractions? Yes; fractional paths are DAY-only, no auctions, no OTO/bracket, DAY stops re-placed daily and reported as `unprotected_overnight`. Chosen automatically below `WHOLE_SHARE_MIN_EQUITY` (5,000).
## 8. Can it trade after hours? No. `allow_extended_hours=false` in both policies; the validator refuses non-limit extended-hours orders anyway.
## 9. Can it trade crypto/options? No code path.

## 10. All risk limits
Policy limits above, enforced in three places: `RiskEngine.admit` (module/symbol allowed, kill switch, daily halt,
entries halted, shorts, overnight, spread cap per ETF/stock, staleness 90 s intraday / 900 s auction, PDT legacy guard);
`Allocator` (per-symbol, per-module, sector 30% of gross, portfolio gross, net bounds, cash when no margin, correlation
haircut 0.7, $1 minimum; scaling new intents only, every scaling logged); `RiskManager.check_order` (the V1 ~25 checks,
unchanged, on every order including protective legs). Throttle: a module whose last 60 trades net negative gets its
risk budget halved; only `risk unthrottle` restores it. Kill switch and daily halt are the V1 ones (20% / 3% in paper
policy; 12% / 2% live). Watchdog thresholds in config/watchdog.yaml.

## 11. Paper Alpaca integration status
V1 loop: verified by the operator (doctor OK, `....ssss` integration, closed-market smoke PASS). V1.5: the REST order
types (limit, stop, OTO, bracket, replace, orders-since) are built from the SDK's request classes and verified against
a stub client; the constraint validator refuses documented violations before any call. Streaming is wrapped alpaca-py
(D25). Gated `tests/integration/test_v15_orders.py` and the first `python -m bot run --env paper --once` are the next
verification steps and belong to the operator.

## 12. Live Alpaca read-only status
Unchanged from FINAL_REPORT.md. Additionally: `live check` shows the policy fingerprint and the promotion gate; `live
arm` refuses while `research/PROMOTIONS.md` has no entry for every module in the live policy (today: refuses).

## 13. Historical-data source status
Daily: as before (Alpaca from 2016, proxies bundled). Minute: `data fetch --timeframe 1m` (Phase 1, resumable, SIP lag
respected); none fetched here. Tier 3 universe: `universe build` from a static candidate list; empty here.
config/earnings.csv is a header only (no earnings source reachable); M3's earnings filter is inert until filled.

## 14. Backtest results
V1 baselines unchanged: SP500 proxy 2000–2022 `ma_crossover` +95.72%, Sharpe 0.51, 26 trades (V1_5_AUDIT.md §2.3),
re-verified after every phase. V1.5 daily-legacy mode reproduces the V1 engine with 0.00e+00 relative difference and
identical trade lists on synthetic, SP500 and GOOG data. **V1.5 modules: no results on real data** — research/RESULTS_V1_5.md
is generated as NOT EVALUATED with the data each module needs. On synthetic random walks all three modules fail the
protocol, as they should.

## 15. Walk-forward results
V1 unchanged: +53.00%, Sharpe 0.55, 56 trades (SP500 proxy). V1.5 replaces hand-chosen grids with declared surfaces
and cuts A/B/C/D; no walk-forward was run for the modules (no data).

## 16. Comparison against buy-and-hold
V1 unchanged (README Evidence table). V1.5: none yet.

## 17. Known bugs fixed (this round)
- `classify_regimes` leaked the full-sample volatility quantile into regime labels (now causal; D17).
- A module exit while a protective stop rested would have been refused by the open-order conflict check (or, without
  the check, could double-fill); the OMS now cancels the protective order first (D9).
- Missed fills looked like orphan positions when reconciliation compared positions before replaying orders since the
  watermark (order fixed).
- A row committed as `submitting` before a crash blocked its slice forever; reconciliation now attaches it to the
  broker order or marks it lost.
- FakeBroker accepted OPG/CLS orders at any time; it now runs the shared validator.
- Auction fill slippage was booked to the overnight attribution bucket (now intraday, D11).

## 18. New tests added
334 offline tests in total (131 new since Phase 1's 203), 12 integration tests (gated). tests/v15 covers fills and
SimBroker, allocator, policy engine, OMS, features, legacy reproduction, minute engine (auctions, gap stops, partials,
priority, serialisation, no-lookahead invariance, cost-stress determinism, early close/holiday, kill switch), modules,
protocol (sealed holdout, unseal log, registry, deflated Sharpe, bootstrap, causal regimes, promotion gate), broker
constraints, store, reconcile, gates/interlock, scheduler, market-data hub, trade updates, daemon, watchdog, and
architecture layering. Full suite green; ruff clean.

## 19. Remaining risks
1. **No V1.5 code has touched Alpaca.** Order-class acceptance (OTO with auction TIFs, nested legs shape, `held` leg
   statuses), the trade-updates frame shape on paper, reconnect behaviour and the 406 frame are all unverified (D3, D22, D25).
2. **No research results.** The modules are untested on real data; the protocol will most likely fail at least M2
   and M3 on cuts B/C. That is the expected outcome of an honest protocol, not a bug.
3. **Cross-module orders on one symbol are serialised, not netted** (D8, D23); with M1 and M2 both on SPY the second
   module's intent is blocked for that event.
4. **The fractional account cannot use auctions or OTO**; every overnight position on a $100 account is
   `unprotected_overnight` between 16:00 and 09:29 (D1).
5. **Watchdog and daemon share the store on one host**; the watchdog's flatten uses `close_all_positions`, which also
   cancels protective stops (by design, but it is the one action that touches them).
6. **Throttle and kill switch are state, not policy**; a deleted `state/*.sqlite` forgets them (the trial registry too).
7. **Time dependence**: the scheduler and the CLS/OPG cutoffs assume a correct clock (chrony) and a correct calendar
   (broker-synced with rule fallback).
8. **Single market-data connection per account**: running `python -m bot run` twice, or beside another tool that
   streams, hits the 406 and the hub stops by design.

## 20. Exact commands to run next (on the Mac, in this order; no `#` comments)
```
git pull origin claude/sweet-heisenberg-x2zz32
git tag v1.5-phase2-backtester 96fc322
git tag v1.5-phase3-research 92fc058
git tag v1.5-phase4-spine 30ce24d
git tag v1.5-phase5-runtime 8abb112
git push origin --tags
python -m pytest -q
python -m bot backtest --strategy ma_crossover --symbol SP500 --start 2000-01-03 --end 2022-12-28 --no-save
python -m bot doctor --paper
python -m bot state migrate --run-id paper
python -m bot data fetch --symbol SPY --symbol QQQ --start 2014-01-01 --end 2026-09-26
python -m bot run --env paper --once
RUN_ALPACA_INTEGRATION=1 RUN_ALPACA_INTEGRATION_ORDERS=1 python -m pytest tests/integration/test_v15_orders.py -q -s
python -m bot watchdog --once
python -m bot research report
python -m bot gates
```
Then, during a market session: `python -m bot run --env paper` in one terminal and `python -m bot watchdog` in another,
for the 60 sessions the gates require. Live remains refused until promotion records exist and `gates` passes.

## WHAT COULD I BE WRONG ABOUT?
- **That wrapping alpaca-py's streams is enough.** Their private hooks (`_start_ws`, `close`, `_dispatch`) could change
  with an SDK upgrade and silently disable reconnect detection; requirements pin 0.44.
- **That the constraint tables are current.** OPG/CLS windows, fractional rules and leg distances were written from the
  documentation as I know it; the integration test is designed to fail loudly where the API disagrees.
- **That serialising same-symbol module orders is safe.** It is conservative, but a blocked M2 exit behind an M1
  rebalance order would delay a flat-by-close rule; the OMS plans exits first, which limits but does not remove this.
- **That the deflated Sharpe from the registry is meaningful** with a handful of trials; it is only as honest as the
  registry is complete, and a deleted registry resets the count (documented as cheating).
- **That the attribution buckets match the spec's intent** (auction slippage as intraday).
- **That `whole_share_min_equity=5000` is the right switch** between the auction and fractional paths.
- **That the watchdog's flatten is the right last resort**; it is off unless listed and only fires on two conditions.
- **That no lookahead remains in the modules**: features are incremental and tested, but M3's "spread None counts as
  eligible" in backtests is more permissive than live admission.
