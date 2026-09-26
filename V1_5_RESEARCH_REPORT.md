# tbot V1.5 Research Report

Prepared 2026-09-26 against commit `2cb338a` of `farhanhaque0619/tbot`.
Scope: audit of the current system, evidence review, Alpaca capability study, microstructure and cost math, and a concrete V1.5 recommendation. A companion file, `V1_5_CODE_PROMPT.md`, turns the recommendation into a build brief for Claude Code.

Nothing here is a promise of return. The word "edge" below means a documented, replicated, cost-adjusted positive expectation, and it is used sparingly.

---

## 1. EXECUTIVE SUMMARY

**What exists today.** A small (5,920 lines), well-tested (162 offline tests passing, 8 integration tests gated behind credentials) daily-bar trading system. It has a no-lookahead event-driven backtester, a shared deterministic `RiskManager` with a 25-check pre-trade gate, an execution loop with idempotent client order ids and crash-safe state, a paper/live credential split with a multi-gate live interlock, a research harness with walk-forward and parameter surfaces, and a shadow-mode advisor that cannot order. The two shipped strategies (50/200 MA crossover and a 20-day z-score reversion) are teaching baselines; the repository itself says so, and the numbers agree: on real Alpaca SPY 2017-2026 the MA baseline made roughly 29% versus 243% for buy-and-hold with about ten trades, and the stitched walk-forward made 12.7% versus 139.7%. Those SPY numbers are from your local run; they are not committed to the repo (the data cache is git-ignored and REPORT.md still reports the close-only proxy), so I have treated them as reported rather than verified.

**Why it is insufficient.** Three reasons, in order of importance.

1. The strategy layer has no evidenced source of return beyond long-run equity beta, and it holds that beta only 30-65% of the time. Everything else in the system is scaffolding around a signal that has nothing to say most days.
2. The risk layer's 2×ATR(14) stop is the dominant decision-maker. On the proxy data 16 of 26 MA exits were stop-outs. My own calculation (section 10) shows a 2% barrier is hit with roughly 84% probability during a 100-day hold in a 1%/day instrument, drift aside. A stop that fires that often is not protection, it is a second, unintended strategy.
3. The architecture is single-strategy, daily, poll-based, and market-order-only. It cannot express intraday intent, cannot hold two strategies' views on one symbol, cannot place a broker-resident stop, and cannot consume streaming data.

**What V1.5 should become.** A multi-strategy platform with a fixed spine (feature engine, TradeIntent bus, allocator, hard risk engine, execution) and three modules that draw on different economic sources and different horizons:

- **M1 Vol-managed index trend (daily, the "carry" sleeve).** Replaces the 50/200 crossover as the beta-timing module: long SPY/QQQ when 12-month time-series momentum is positive, sized so that ex-ante portfolio volatility hits a target. This is the part of the trend-following literature that survived the replication fights (the volatility scaling), rebalanced weekly. Low turnover, few trades, most of the long-run return.
- **M2 Market intraday momentum (intraday, the "flow" sleeve).** On SPY and QQQ, the sign and size of the first half-hour return (prior close to 10:00) and the 15:00-15:30 return predict the last half-hour. Documented in the Journal of Financial Economics twice, with an economic mechanism (gamma hedging and end-of-day rebalancing flows). One 30-minute trade per ETF per day, entered at 15:30 with a marketable limit, exited at the close. Long-only in initial live; the short leg is enabled in paper research only.
- **M3 Large-cap residual reversal (multi-day, the "liquidity provision" sleeve, PAPER-ONLY in V1.5).** On ~30 of the most liquid US large caps, buy the names whose 5-day market-adjusted return is most negative, hold 1-5 days, exit on reversion or time. Tier A evidence that it exists in large caps, Tier A evidence that naive implementations lose to costs, so it must earn promotion through the protocol in section 11.

Around those modules: a 30-minute decision cadence fed by Alpaca WebSocket bars; a TradeIntent schema so strategies express opinions and never sizes; a volatility-normalised allocator with gross, net, symbol, and strategy caps; the existing `RiskManager.check_order` preserved as the final gate; broker-side OTO/bracket protective stops for whole-share positions and re-placed daily stop orders for fractional ones; an autonomous daemon under systemd with a separate watchdog; and a research protocol that reserves untouched holdout data and reports deflated Sharpe ratios.

Expected shape, with the caveats in section 28 of the brief: 1-3 orders per day in live (M2 daily, M1 a few times per month), 3-8 per day once M3 is promoted; gross exposure 0-100% of equity, net long only in V1.5 live; and no judgement about whether it "works" before roughly 500 M2 trades, which is about two years, or roughly 350 M3 trades.

---

## 2. CURRENT BOT ARCHITECTURE

Verified by reading every module under `bot/`, running the test suite, and reading the nine markdown documents. Line counts are from `wc -l`.

```
                          bot/config.py (Settings: env, paper/live keys, risk, costs, flags)
                                          |
   +------------------+     +-------------+--------------+     +------------------------+
   | bot/data          |     | bot/strategies             |     | bot/research (cannot    |
   | providers.py     |     | base.py  Strategy/Bar/Signal|     |  order; import-scan     |
   |  Alpaca daily     |---->| ma_crossover.py            |     |  tested)                |
   |  CSV              |     | mean_reversion.py          |     | harness/surface/kelly/  |
   | store.py DuckDB   |     | indicators.py (O(1))       |     | candidates/review       |
   | quality.py        |     +-------------+--------------+     +------------------------+
   | calendar.py       |                   | Signal(target in {-1,0,1})
   | loader.py         |                   v
   +--------+----------+     +-------------+--------------+
            |               | bot/risk                    |
            |               | manager.py RiskLimits,       |
            |               |   SafeLiveLimits, RiskState, |
            |               |   RiskManager.update_equity, |
            |               |   can_open, stop_distance,   |
            |               |   position_qty, check_order  |
            |               | sizing.py fixed_fractional   |
            |               +-------------+--------------+
            |                             |
   +--------v----------+     +-------------v--------------+     +------------------------+
   | bot/backtest       |     | bot/execution               |     | bot/monitoring          |
   | engine.py (daily,  |     | paper_loop.py Trader        |     | logging (redaction)     |
   |  fill next open,   |     |  (poll, replay, reconcile,  |     | decisions.jsonl         |
   |  stop on close)    |     |   submit market order)      |     | alerts (Discord)        |
   | costs.py (bps)     |     | broker.py Alpaca/Protocol   |     | health, dashboard       |
   | walkforward.py     |     | fake_broker.py              |     +------------------------+
   | metrics.py         |     | interlock.py (live gates)   |
   +-------------------+     | state.py (atomic JSON)      |     +------------------------+
                             | market_state.py             |     | bot/advisor (shadow)    |
                             | smoke.py (paper only)       |     | base/jev/shadow         |
                             +----------------------------+     +------------------------+
```

**Data.** `AlpacaBarProvider.fetch_daily` pulls daily bars only (`TimeFrame.Day`), split-adjusted, SIP with IEX fallback, never past now minus 16 minutes. Cached in DuckDB by `BarStore`. `quality.py` checks gaps, duplicates, OHLC consistency, splits. DATA.md correctly documents the Alpaca 2016 history floor. There is no minute-bar path, no quote/trade history, no streaming client.

**Strategies.** `Strategy.on_bar(Bar) -> Signal | None`, `Signal.target in {-1,0,1}`, optional `stop_price`. State is not persisted; the trader re-derives it by replaying the warm-up window plus recorded `risk_exits`. `on_position_closed` lets a strategy re-arm after a stop. Only two production strategies; three research candidates in `bot/research/candidates.py` are deliberately non-executable.

**Risk.** `RiskLimits` (1% risk per trade, 50% max position, 3% daily halt, 20% drawdown kill, 5 positions, 2×ATR stop, 50 bps max spread, 900 s max staleness) and `SafeLiveLimits` ($25 order, $50 gross, $5 day, $10 drawdown, 1 position, SPY/QQQ). `check_order` is a flat list of ~25 named boolean checks returning the first failure. It is deterministic and shared by backtest and execution. There is no notion of gross/net exposure caps outside safe mode, no per-strategy or per-sector concentration limit, and no short permission separate from the account flag.

**Portfolio handling.** None. One strategy instance per symbol; the "portfolio" is whatever the position cap allows. Backtester and trader both queue at most one order per symbol per bar.

**Execution.** `Trader.run_cycle`: sync orders, pull account/positions, reconcile, feed equity to risk, find last completed session, replay strategy, compare desired to actual, submit one market order (OPG for whole shares in the 19:00-09:28 window, DAY while open for fractional). Client ids are `<run>-<sym>-<date>-<entry|exit>`. State is persisted before and after submission. Retries with backoff on 429/5xx. Partial fills booked. Reconciliation trusts the broker and adopts unknown positions without a stop.

**Broker.** `Broker` protocol with `submit_market_order` only. `AlpacaBroker.for_env` verifies base URL both ways and account-number shape (`PA` prefix). No limit, stop, bracket, or replace methods. No streaming.

**State.** `BotState` JSON with atomic writes: `last_processed`, `orders`, `positions` (with software stop), `risk`, `risk_exits`, `trades`, `equity_log`. Single-process assumption documented.

**Paper/live safety.** Separate key pairs with prefix checks; `TRADING_ENV`; `--live`; `live arm` with typed phrase, 30-minute TTL, fingerprint of safe limits; `LIVE_AUTONOMOUS_TRADING=false` default; interlock gates listed in `interlock.py`. This is good and should be kept intact.

**Research.** `compare` (cash, B&H, baselines, candidates over IS/validation/holdout cuts, regime table, walk-forward), `surface` (parameter grid with neighbour ratio), `kelly` (bounded, research-only), `review` (read-only post-session). Regime labels use a full-sample volatility quantile (documented leakage). Walk-forward grids were hand-chosen with knowledge of the sample.

**Deployment.** None. A `python -m bot paper` loop on whatever machine runs it, with `Ctrl-C` shutdown. No supervisor, no watchdog, no heartbeat.

**What I could not verify.** Anything that requires credentials: the paper smoke test (`python -m bot smoke`, commit 2cb338a) and the SPY/QQQ numbers you quoted. The commit history shows those were added on 09-26 after FINAL_REPORT.md was written on 09-24, which is consistent with your account.

---

## 3. CURRENT STRATEGY: EXACTLY WHAT IT DOES

**`ma_crossover` (fast=50, slow=200, long/flat).** After 201 daily closes, target is +1 when SMA50 > SMA200 else 0; a Signal is emitted only on change. Sized at 1% of equity against a 2×ATR(14) stop, capped at 50% of equity and by cash. Exited by the strategy when the cross reverses, or by the risk layer when a daily close breaches the stop, after which `on_position_closed` resets state and the strategy re-enters at the next bar if SMA50 is still above SMA200.

- Trade frequency: about one round trip per year from the signal itself; several more from stop-and-re-enter cycles.
- Horizon: months to years. A 200-day average lags a turning point by roughly 100 days.
- Edge hypothesis: slow trend persistence in the index, or equivalently avoiding the deepest part of bear markets. The literature supports the second reading (drawdown reduction) much more than the first (return enhancement), and the evidence in the repo matches: lower drawdown, much lower return, Sharpe roughly equal to buy-and-hold.
- Shortcomings: 10-15 out-of-sample trades give no statistical power at all (section 10); the stop dominates behaviour; sizing against a 2% stop at 1% risk puts ~50% of equity to work at most, so the strategy cannot even collect the full equity premium during the 65% of the time it is long.

**`mean_reversion` (lookback=20, entry_z=2.0, exit_z=0.5, long only).** z = (close - SMA20)/std20 over the same window. Enter long when z < -2, exit when z > -0.5. Optional short mirror exists but has never been backtested with costs and is blocked in safe mode.

- Frequency: roughly 7 round trips per year on the index; 12-14% time in market.
- Horizon: days.
- Edge hypothesis: short-term index reversal after a two-sigma drop. That is a real phenomenon in the cross-section of stocks (section 4) and weak-to-absent at the index level after 2000; the repo's own surface shows a fragile parameter landscape and break-even at 3 bps per side.
- Shortcomings: a 20-day window is short enough that std20 collapses in calm periods and the -2 threshold fires on noise; the exit at -0.5 leaves most of any reversion on the table; the ATR stop, again, fires first in exactly the high-volatility conditions where reversal returns are largest (Nagel 2012).

Neither strategy deserves capital in its current form. Both deserve to remain as benchmarks, which section 17 and the code prompt require.

---

## 4. LITERATURE REVIEW

Tiering used throughout: **A** replicated academic evidence in a top journal or equivalent, **B** serious practitioner or working-paper research with reproducible method, **C** credible engineering experience, **D** anecdote. Citations are to the published version where one exists; SSRN links are given where the published version is paywalled.

### 4.1 Summary table

| # | Strategy / anomaly | Key evidence | Replication and decay | Turnover | Cost sensitivity | Alpaca feasibility | Verdict for V1.5 |
|---|---|---|---|---|---|---|---|
| 1 | Time-series momentum (12-1) | Moskowitz, Ooi, Pedersen 2012 JFE [S1] | Contested: Kim, Tse, Wald 2016 [S2] show the profit is mostly volatility scaling; Huang et al. 2020 JFE [S3] find weak asset-by-asset evidence; Hurst et al. 2017 [S4] find a century of positive net returns | Low (monthly) | Low | Yes, ETFs, long-only fine | **Adopt the vol-scaling part (M1)**, treat the sign filter as a drawdown control not an alpha |
| 2 | Cross-sectional momentum (12-1) | Jegadeesh-Titman 1993 JF; Asness-Moskowitz-Pedersen 2013 | Robust gross; Novy-Marx-Velikov 2016 RFS [S5] and Chen-Velikov 2023 JFQA [S6] show net returns near zero post-2005 for average implementations; momentum crashes (Daniel-Moskowitz 2016) | Medium | Medium-high | Needs 100+ names and shorts for the classic form | Not for V1.5; too capital-hungry, crash-prone, and slow to evaluate |
| 3 | Short-term reversal (1 week / 1 month) | Jegadeesh 1990, Lehmann 1990; Nagel 2012 RFS [S7] links it to liquidity provision and VIX | Avramov-Chordia-Goyal 2006 JF [S8]: concentrated in illiquid names; de Groot-Huij-Zhou 2012 [S9]: survives costs in the largest 100-500 stocks with smart turnover control | Very high (weekly ~300-800%) | Very high | Yes in large caps; shorts optional | **Paper-only module M3**, long side of large-cap residual reversal, promote only on evidence |
| 4 | Intraday mean reversion (VWAP deviation etc.) | Heston-Korajczyk-Sadka 2010 JF [S10]: half-hour periodicity and reversal driven by liquidity imbalances under one hour and bid-ask bounce | Effect is real but the profit is largely the effective spread | Extreme | Extreme | Technically yes; economically no at our latency | Reject as a standalone; the periodicity finding informs execution timing only |
| 5 | Opening-range breakout | Zarattini-Aziz 2023 SSRN (Tier B) [S11] | No independent peer-reviewed replication; results rely on leveraged instruments and aggressive sizing | High (daily) | High | Yes | Research candidate only; not in V1.5 core |
| 6 | Market intraday momentum (first half-hour predicts last) | Gao, Han, Li, Zhou 2018 JFE [S12]: SPY 1993-2013, R² 1.6%, stronger on volatile and high-volume days; Baltussen, Da, Lammers, Martens 2021 JFE [S13]: 60+ futures 1974-2020, mechanism is short-gamma hedging demand | Replicated internationally (Li-Sakkas-Urquhart 2022 JFM; Ho-Lv-Schultz 2021); mechanism gives a reason to expect persistence; still a post-publication effect | One trade per day per ETF | Low on SPY/QQQ (1-2 bps round trip with limits) | Yes; long side needs nothing, short side needs $2k margin | **Adopt (M2)** |
| 7 | Overnight vs intraday decomposition | Lou-Polk-Skouras 2019 JFE [S14]: firm-level overnight continuation, intraday continuation, cross-period reversal; anomaly profits accrue either entirely overnight or entirely intraday | Bogousslavsky 2021 JFE confirms; the "overnight drift" in index ETFs is real but small per night and eaten by two crossings of the spread | Daily | High for single names; moderate for ETFs | Yes | Use as a research lens (attribute every module's P&L to overnight vs intraday); not a module |
| 8 | Gap behaviour (fade / follow) | Mostly practitioner; academic support is indirect via #7 and via Berkman et al. 2012 (retail attention at the open) | Weak, regime-dependent | Daily | High (opening spread and auction impact) | Yes | Reject for V1.5 |
| 9 | Post-earnings announcement drift | Bernard-Thomas 1989; large literature | Decayed substantially in large caps (Martineau 2022 "Rest in Peace PEAD" finds it gone in large, liquid stocks after 2006) | Event-driven | Medium | Needs earnings calendar and surprise data we do not have | Reject for V1.5 |
| 10 | Volatility scaling / vol-managed portfolios | Moreira-Muir 2017 JF [S15]: scaling exposure by inverse recent variance raises Sharpe for the market and factors; Barroso-Santa-Clara 2015 for momentum | Cederburg et al. 2020 JFE show it is not a free lunch for a mean-variance investor with real-time estimation, but drawdown reduction holds | Low-medium | Low | Yes | **Adopt inside M1 and in the allocator** |
| 11 | Volatility breakout / Donchian | Practitioner; the repo's H1 tested it (Sharpe 0.38, below MA) | No | Medium | Medium | Yes | Reject (already tested here and lost) |
| 12 | Trend following (broad) | Hurst-Ooi-Pedersen 2017 JPM [S4] | Positive net across a century, most value in crises; low Sharpe in isolation on a single equity index | Low | Low | Yes | Covered by M1 |
| 13 | VWAP mean reversion | Overlaps #4 | Same | Extreme | Extreme | Same | Reject |
| 14 | Relative strength (ETF/sector rotation) | Overlaps #2; sector momentum documented (Moskowitz-Grinblatt 1999) | Post-2005 net returns thin | Monthly | Low-medium | Yes, 11 sector ETFs fit inside the 30-symbol Basic cap | V1.6 candidate for M1's universe, not V1.5 |
| 15 | Pairs / stat arb | Gatev-Goetzmann-Rouwenhorst 2006 RFS; Do-Faff 2010 show decay | Decayed to near zero net in liquid US equities; needs shorts and many pairs | High | High | Needs $2k margin and ETB names | Reject for V1.5 |
| 16 | Lead-lag (large to small, index to constituents) | Lo-MacKinlay 1990; modern versions operate at sub-second horizons | Arbitraged at high frequency | Extreme | Extreme | No (latency) | Reject |
| 17 | Volume / liquidity signals | Volume conditions #6 (Gao et al.) and #3 (Nagel) | Conditioning, not standalone alpha | n/a | n/a | Volume needs SIP (IEX volume is a few percent of the tape) | Use as conditioning features only |
| 18 | Order-flow imbalance | Cont-Kukanov-Stoikov 2014 and successors: predictive at seconds | Real, but decays within seconds | Extreme | Extreme | No (needs full quotes and sub-second reaction) | Reject |
| 19 | Volatility clustering | Engle 1982 and everything since | Universally replicated | n/a | n/a | Yes | Feature (realised vol forecasts drive sizing everywhere) |
| 20 | Regime dependence | Ang-Bekaert 2002; practitioner consensus; the repo's own regime table | Regimes are identifiable ex post, weakly ex ante | n/a | n/a | Yes | Deterministic vol/trend state in the allocator; no discrete regime "prediction" |
| 21 | Market breadth | Practitioner; weak academic support at short horizons | Weak | Daily | n/a | Needs constituent data beyond the 30-symbol cap | Reject for V1.5 |
| 22 | ETF/index microstructure (closing auction) | Bogousslavsky-Muravyev 2023 (closing auction volume and price impact); Ernst-Sokobin-Spatt on closing auction price impact [S16] | Closing auction is the cheapest venue for size in large caps | n/a | n/a | Alpaca supports MOC/LOC (`cls`) for whole shares | Use CLS for M2 exits on whole-share positions |
| 23 | Cross-asset signals (VIX, rates, credit) | Nagel 2012 (VIX conditions reversal); Baltussen et al. (gamma) | Conditioning | n/a | n/a | VIX index values are not in the Alpaca equity feed; VIXY/VXX proxies are | Optional conditioning; not required |
| 24 | Intraday seasonality | Heston-Korajczyk-Sadka 2010 [S10]; Andersen-Bollerslev 1997 volatility U-shape | Robust for volatility, weak for returns | n/a | n/a | Yes | Feature (time-of-day vol normalisation) |
| 25 | Overnight anomaly (close-to-open in indexes) | Cooper-Cliff-Gulen 2008; Lou et al. [S14] | Real but small per night; ETF implementation pays the spread twice daily | Daily | High | Yes | Reject as a module |
| 26 | Day-of-week, turn-of-month | Old literature, mostly gone in liquid US equities post-2000 | Decayed | Low | Low | Yes | Reject (data-mined) |
| 27 | Transaction-cost decay | Novy-Marx-Velikov 2016 [S5]; Chen-Velikov 2023 [S6] | Average anomaly nets ~4 bps/month after costs and decay; best ~10 bps | n/a | n/a | n/a | Governs everything: assume half of any published gross effect and full costs |
| 28 | Post-publication decay | McLean-Pontiff 2016 JF [S17]: 26% lower out of sample, 58% lower post-publication | Replicated (Jacobs-Müller 2020; Chen-Zimmermann 2022; Falck et al. 2021) | n/a | n/a | n/a | Same as 27 |

### 4.2 What the table says when read as a whole

Three findings dominate the design.

First, **costs and decay have already removed the average published anomaly.** Chen and Velikov, using 204 anomalies, effective spreads, post-publication windows and post-2005 data, find the average long-short anomaly nets about 4 bps per month and the best about 10 bps after controlling for data mining [S6]. McLean and Pontiff's 58% post-publication decay is one of the most replicated results in the field [S17]. The practical rule is: take any published gross effect, halve it, subtract full round-trip costs, and only then ask whether it is worth building.

Second, **the intraday effects that survive that treatment have a mechanism.** Gao et al. document that the first half-hour return predicts the last half-hour return in SPY with a predictive R² of 1.6%, stronger on volatile and high-volume days [S12]. Baltussen et al. find the same pattern in more than 60 futures across four asset classes since 1974 and tie it to hedging of short gamma exposure by option market makers, leveraged ETFs and portfolio insurers, whose rebalancing must trade in the direction of the day's move near the close [S13]. A flow that is mechanically required is more likely to persist than a behavioural mispricing that arbitrage capital can remove. This is the strongest single candidate for a small Alpaca account because it is on the most liquid instrument in the world, once a day, for thirty minutes.

Third, **short-term reversal is a real return to liquidity provision, and the cost structure decides who gets to collect it.** Nagel shows reversal returns track VIX because they are compensation for supplying immediacy when intermediaries pull back [S7]. Avramov, Chordia and Goyal show the naive strategy's profit sits in illiquid names [S8]; de Groot, Huij and Zhou show that restricting to the largest 500 or 100 names and controlling turnover keeps it positive after realistic costs [S9]. Lou, Polk and Skouras show reversal's profit accrues overnight, not intraday [S14], which tells you when to hold it (across closes, not within a session). It is worth a paper-only module precisely because the evidence is mixed and the cost sensitivity is extreme.

What does not survive: anything at sub-minute horizon (order flow, lead-lag, VWAP scalping), anything requiring data we lack (PEAD, breadth), anything requiring a large short book (classic cross-sectional momentum, pairs), and calendar effects.

### 4.3 Feature-by-feature justification for the adopted modules

Every feature in V1.5 must answer five questions (Part 11 of the brief).

**M1: realised-volatility scaled index exposure with a 12-month sign filter**
- Feature: 21- and 63-day realised volatility of daily returns; 12-month total return excluding the last month.
- Hypothesis: expected return is not proportional to variance in the short run, so scaling exposure by inverse recent variance raises the Sharpe of a long index position and cuts drawdowns [S15]; the 12-month sign filter keeps the position out of extended bear markets [S1, S4].
- Evidence: Moreira-Muir 2017 JF; Hurst-Ooi-Pedersen 2017; the repo's own H3 (vol filter cut drawdown on the proxy).
- Failure mode: V-shaped recoveries that start in high volatility (2009, 2020), when the strategy is smallest; whipsaw around the 12-month sign in sideways markets.
- Execution implication: signal decays over weeks; rebalance weekly or when target weight drifts more than 10% of equity; OPG or CLS orders are fine.

**M2: market intraday momentum**
- Feature: r1 = log(P10:00 / Pprev_close); r12 = log(P15:30 / P15:00); 21-day realised vol of the last-half-hour return for thresholding; day's volume relative to its 21-day median (SIP only).
- Hypothesis: end-of-day hedging and rebalancing flows push the close in the direction of the day's move; late-informed traders act near the close [S12, S13].
- Evidence: two JFE papers plus international replications.
- Failure mode: days with a large intraday reversal (macro shock in the afternoon); low-volatility days where the predicted move is below cost; the effect weakening as it is traded.
- Execution implication: the signal is known at 15:30 and the position is closed at 16:00. Entry with a marketable limit at 15:30-15:31; exit via MOC (`cls`, whole shares) or a market order at 15:58 (fractional). Any latency under a minute is irrelevant.

**M3: large-cap residual reversal (paper-only)**
- Feature: 5-day return minus beta times the 5-day SPY return (residual), ranked across the universe; 21-day residual volatility; spread as a fraction of price from the latest quote.
- Hypothesis: temporary price pressure from liquidity demanders in the largest names reverts over days; the return is compensation for providing liquidity [S7].
- Evidence: Tier A on existence, Tier A on cost sensitivity, Tier B on survivability in large caps [S8, S9].
- Failure mode: the losers are losers for a reason (news), so a news filter or an earnings-date exclusion is needed; costs above about 10 bps round trip make it negative; momentum crashes hit the long-loser side hardest.
- Execution implication: enter at the next open via OPG (whole shares) or a limit near the open (fractional); exit on reversion of the residual or after 5 days; overnight holding is required because the profit accrues overnight [S14].

---

## 5. PRACTITIONER, X, AND GITHUB FINDINGS

Separated by tier. I did not find, and would not accept, any P&L screenshot as evidence.

**Tier B (reproducible practitioner research).**
- Zarattini, Aziz and Barbon 2024 (Swiss Finance Institute working paper, SSRN 4824172) extend the academic last-half-hour result into a full-day trend-following rule on SPY: enter when price leaves a "noise band" around the open scaled by the average intraday move, trail with VWAP, exit at the close [S18]. They report costs and slippage explicitly. Independent reimplementations on TradingView report returns smaller than the paper and a much lower Sharpe once margin is checked, and one author found and fixed a calculation error that had inflated results [S19]. Read: the direction is consistent with Gao et al., the magnitude is not established, and the leverage assumptions are aggressive. Useful as a research candidate (M2b) with the last-half-hour rule as the conservative core.
- The same group's ORB paper (2023) makes claims on TQQQ with heavy leverage; no independent replication of comparable quality. Tier B at best, and the leverage makes it unsuitable for our risk policy.

**Tier C (engineering lessons, consistent across many sources).**
- Alpaca's paper environment simulates fills against real-time quotes and does not route to exchanges, and Alpaca itself warns that active markets can make results diverge [S20]. Forum measurements report materially different latency between paper and live [S21]. Implication: paper trading validates plumbing and signal timing; it does not validate fill quality. Slippage must be measured in live with tiny size, which the repo already logs (`realized_slippage_bps`).
- Trade updates should be consumed over the `trade_updates` WebSocket rather than polled; Alpaca's own docs call streaming the recommended way to maintain order state [S22]. Every serious open-source Alpaca client implements reconnection with exponential backoff and falls back to polling `GET /v2/orders` with a watermark [S23].
- The Basic data plan allows one concurrent stock WebSocket connection and 30 symbols; exceeding either returns an in-band error [S24]. This is a hard design constraint for the universe (section 9).
- Common retail-bot failure modes reported repeatedly on the Alpaca forum and in project post-mortems: double orders after a restart, positions with no exit after a crash, stale-quote sizing, treating IEX volume as market volume, PDT flags on accounts under $25k, and fractional-order rules (DAY only, no bracket) surprising people in live.

**Tier D (ignored).** X/Twitter accounts publishing strategy P&L, "AI trading bot" repositories with no cost model, and anything claiming a Sharpe above 3 on daily bars.

---

## 6. ALPACA CAPABILITIES

Read from the current docs (pages modified April-August 2026). The distinction requested in Part 5 is made explicit in the last column.

| Capability | Documented status | Our account today (Basic plan, ~$100 live, paper) | Needs upgrade? |
|---|---|---|---|
| Historical bars (1m to 1d) | Since 2016, split/dividend adjustment flags, SIP or IEX feed | Yes, but SIP data in the most recent 15 minutes is refused on Basic; IEX is real-time | Algo Trader Plus ($99/mo) removes the 15-minute SIP restriction and raises REST limits from 200 to 10,000 per minute [S25] |
| Latest quotes / trades | Yes | IEX only in real time on Basic | Plus for SIP NBBO |
| WebSocket market data | Trades, quotes, minute bars, updated bars, daily bars, statuses | One connection, 30 symbols on Basic | Plus for unlimited symbols [S25] |
| WebSocket trade updates | `wss://paper-api.alpaca.markets/stream` and `wss://api.alpaca.markets/stream`, `trade_updates` stream, fills, partials, cancels, rejects [S22] | Yes | No |
| Market, limit, stop, stop-limit | Whole shares: DAY and GTC; IOC/FOK/OPG/CLS for market and limit [S26] | Yes | No |
| Fractional orders | Market, limit, stop, stop-limit, DAY only; no GTC, no OPG/CLS, no shorts; fill priced at NBBO with no price improvement [S27] | Yes (asset must be `fractionable`) | No |
| Bracket / OCO / OTO | Supported for whole-share orders; take-profit limit plus stop or stop-limit; DAY or GTC; no extended hours; bracket legs are sent DNR/DNC; stop must be at least $0.01 from the base price; OCO is exit-only [S26] | Whole shares only; not available for fractional quantities (forum confirmation, and the fractional order-type table has no order_class column) | No, but it means a $100 account cannot use brackets |
| Trailing stop | Whole shares, DAY or GTC, regular hours only, not yet as a bracket leg [S26] | Yes | No |
| Auction orders | OPG (submit before 09:28 or after 19:00), CLS (before 15:50 or after 19:00), routed to the primary exchange [S26] | Whole shares only | No |
| Extended hours | Limit orders only, DAY or GTC, 04:00-09:30 and 16:00-20:00, plus overnight 20:00-04:00 for eligible assets [S26] | Yes | No; V1.5 policy is off |
| Short selling | ETB assets only; HTB not supported; an open short in a name that becomes HTB is cancelled before the open; $0 borrow fee on ETB; requires $2,000 equity [S28, S29] | Not available with $100 equity | Fund to $2k |
| Margin | 2x for accounts at or above $2,000; overnight maintenance 30% on most longs; PDT accounts at $25k get 4x intraday [S28] | Not available | Fund to $2k |
| PDT | Legacy rule: 4 day trades in 5 business days flags accounts under $25k. FINRA's replacement (intraday margin, Rule 4210 amendments) removes the $25k threshold with a 12-month transition during which firms may apply either regime [S30] | Must be verified on the account (`pattern_day_trader`, `daytrade_count`) before enabling any intraday module in live | Not a plan issue; a regulatory timing issue |
| Rate limits | Trading API 200 req/min; Market Data 200 (Basic) or 10,000 (Plus) [S24, S25] | 200/min | Plus for a large universe |
| Corporate actions | Adjustment flags on historical bars; GTC limit orders are price-adjusted; brackets are not | Yes | No |
| Calendar / clock | `/v2/clock`, `/v2/calendar` with early closes | Yes (repo already normalises naive SDK datetimes) | No |
| Idempotency | `client_order_id` unique per account; duplicate rejected | Yes (already used) | No |

**Consequences for V1.5.**
1. The universe is capped at 30 streamed symbols on Basic. V1.5 is designed to fit inside that cap; Plus is recommended once live capital makes $99/month a rounding error (under 1%/year at $12k).
2. Volume-based features cannot use IEX volume. Relative-volume conditioning in M2 is computed from SIP minute bars fetched with a 16-minute lag, which is acceptable because M2 needs the day's volume up to 15:15, not to 15:30.
3. Broker-resident protection (bracket/OTO) exists only for whole-share positions. Fractional positions can carry a DAY stop order that must be re-placed each morning and does not exist overnight. The risk engine must know which regime a position is in.
4. Shorts and margin need $2,000. V1.5 live is long-only until funded; the code must support shorts in paper.
5. The PDT status of the live account must be a hard gate: M2 makes one day trade per ETF per day, which flags a sub-$25k account in three days under the legacy rule. The correct mitigation is to check whether Alpaca has moved the account to the intraday-margin regime, and if not, to limit M2 to one day trade per rolling five sessions (which nearly kills it) or to fund past $25k, or to route M2 exits through the next morning (which changes the strategy and invalidates its evidence). The honest answer is that **M2 cannot run in a $100 live account under the legacy PDT rule**; it runs in paper until the account regime or size changes. This is a finding of the audit, not a footnote.

---

## 7. MICROSTRUCTURE ANALYSIS

The question is which horizons a normal API user, with roughly 100-500 ms round-trip latency to Alpaca, IEX quotes, and market or marketable-limit orders, can compete at.

**Cost anatomy per round trip, our situation.**
- SPY, QQQ: quoted spread is normally one cent on a price in the hundreds of dollars, so half-spread is well under 1 bp per side. Marketable limit orders at the touch fill immediately in normal conditions. Realistic round-trip implementation shortfall: 1-3 bps, more at the open and in fast markets.
- Top-30 large caps: quoted spreads of 1-3 bps, so 1-3 bps round trip in spread plus 2-5 bps of adverse selection and timing. Realistic: 4-10 bps.
- Sector ETFs: 1-5 bps spreads, thinner books; 4-12 bps.
- Regulatory fees (SEC Section 31 and FINRA TAF on sells) are on the order of 0.1-0.3 bps at current rates; commissions are zero. Fractional fills get no price improvement and are priced at the NBBO, so fractional execution is at least the full half-spread per side.

**The fundamental constraint: signal scales with the square root of horizon, cost does not.** With daily volatility near 1%, the standard deviation of a 5-minute SPY return is about 11 bps; of a 30-minute return about 28 bps; of a 2-hour return about 55 bps; of a 5-day return about 224 bps. A strategy that captures a fixed fraction of the horizon's move (an information coefficient) earns an expectation proportional to the horizon's standard deviation, but pays the same 2-10 bps regardless. Section 10 makes this quantitative: at 5 minutes a symmetric bet must win 59% of the time just to break even against 2 bps; at 30 minutes 53.6%; at a day 51%.

**Adverse selection.** A resting limit order in SPY is filled preferentially when the market is about to move against it. Because V1.5 uses marketable orders at known times (15:30, the open, the close) rather than resting quotes, adverse selection enters as the cost of crossing the spread rather than as picked-off fills, which is the cheaper of the two for a slow trader.

**Auctions.** The closing auction is the deepest liquidity event of the day and has lower price impact than the continuous market for all but microcaps [S16]. M2 exits and M1 rebalances should use CLS when the position is in whole shares. The opening auction is comparatively illiquid and the first minutes after it are the widest-spread, highest-volatility part of the day, which is why M3 entries use OPG (in the auction) rather than market orders at 09:31.

**Stops.** A stop is elected on the consolidated print within the NBBO and becomes a market order [S26]. Slippage on stop elections in liquid ETFs during regular hours is small; across a gap it is the whole gap. No stop protects against overnight gaps, only position size does.

**Quote staleness.** On Basic, quotes are IEX only. IEX quotes are usually at or near the NBBO for SPY and QQQ but can be a level away in single names. The risk gate already refuses to size against a stale quote; V1.5 additionally cross-checks the IEX mid against the last SIP minute bar (lagged) and against the day's realised range, and refuses if the deviation is implausible.

**What is tradeable for us.** Horizons from about 30 minutes to several weeks, with entries and exits at scheduled times or in auctions, on instruments where the round-trip cost is under about 5 bps (ETFs) or 10 bps (top large caps). Not tradeable: anything under about 15 minutes, anything requiring resting quotes, anything in names with spreads above ~10 bps.

---

## 8. STRATEGY CANDIDATE COMPARISON

Evaluated on the criteria the brief lists in Part 3 and Part 8. Scores are qualitative (strong / ok / weak / fail) and cost figures use the section 7 estimates.

| Candidate | Evidence tier | Mechanism | Gross edge / trade (post-decay guess) | Round-trip cost | Trades / yr | Sample to judge | Data needed | Short needed | Alpaca fit | Decision |
|---|---|---|---|---|---|---|---|---|---|---|
| A. Vol-managed index trend (M1) | A (vol scaling), A-contested (sign) | Variance is forecastable, expected return is not; bear-market avoidance | n/a per trade; +0.1 to +0.2 Sharpe over B&H with 30-50% lower drawdown | 2-4 bps, negligible | 10-30 | Years (few trades but daily returns are the unit) | Daily SIP | No | Excellent | **Core** |
| B. Market intraday momentum (M2) | A | Gamma hedging, late-day rebalancing | 2-5 bps on SPY, higher on QQQ | 1-3 bps | ~250 per ETF | ~500 trades (2 yrs) | Minute IEX real-time, SIP lagged | No (long-only variant loses about half the signal days) | Good, except PDT | **Core** (paper until PDT resolved) |
| B2. Zarattini noise-band intraday trend | B | Same as B, extended to whole day | Unknown; independent replications lower | 2-6 bps | ~200 | ~500 | Same as B | Optional | Good, same PDT issue | Research candidate |
| C. Large-cap residual reversal (M3) | A / A (costs) | Liquidity provision | 10-30 bps over 1-5 days, before costs, in the largest names | 6-12 bps | 100-300 (long side, ~30 names) | ~350 trades | Daily SIP, quotes | No for long side | Good | **Paper-only core** |
| D. Cross-sectional 12-1 momentum | A | Underreaction / flows | ~40 bps/month gross post-decay | 20-40 bps/month | Monthly | 5+ years | 100+ names | Yes for the classic form | Poor under 30-symbol cap | Reject for V1.5 |
| E. Opening-range breakout | B | Range expansion after the open | Unknown | 3-8 bps, highest at the open | ~250 | ~500 | Minute real-time | No | PDT issue | Reject for V1.5 |
| F. Intraday VWAP reversion | A (existence) | Bid-ask bounce and sub-hour imbalance | ~ spread | ~ spread | Thousands | Thousands | Quotes | No | Fails at our latency | Reject |
| G. Overnight ETF drift | A (existence) | Retail attention at the open | 1-3 bps per night | 2-4 bps | 250 | Thousands | Daily | No | Fine | Reject (cost ≥ edge) |
| H. Pairs / stat arb | A (historical), decayed | Convergence | Near zero net post-2010 in liquid US names | 10-20 bps | High | Years | Many pairs | Yes | Needs margin | Reject |
| I. PEAD | A, decayed in large caps | Underreaction to earnings | Small in large caps | 6-12 bps | Event-driven | Years | Earnings surprises | No | Data not available | Reject |
| J. Sector rotation | A-weak | Sector momentum | Thin | 4-12 bps | Monthly | Years | 11 sector ETFs | No | Fits the cap | V1.6 candidate |

Why A + B + C beat the alternatives: they use three different economic sources (risk premium timing, mechanical end-of-day flow, liquidity provision), three different horizons (weeks, 30 minutes, days), and the returns to B and C are expected to be uncorrelated with each other and only weakly correlated with A. Reversal and momentum are classically negatively correlated, which is the diversification the brief asks for. They fit in 30 symbols, need no shorts in live, need no data beyond what Basic provides (with the lagged-SIP workaround), and each has a documented failure regime that can be monitored.

Why not more: every additional module costs research time, adds trials to the multiple-testing count (section 10), and shares the same execution pipe. Three is enough to prove the platform.

---

## 9. RECOMMENDED V1.5 STRATEGY

This section is the specification. The code prompt references it by module id.

### 9.1 Universe

Fixed at start, reviewed quarterly by the operator, never by the bot.

- **Tier 1 (index ETFs, 4):** SPY, QQQ, IWM, DIA. M1 trades SPY and QQQ; M2 trades SPY and QQQ; IWM and DIA are streamed for features and research.
- **Tier 2 (sector ETFs, 11):** XLK, XLF, XLE, XLV, XLY, XLP, XLI, XLU, XLB, XLRE, XLC. Streamed for the residual model's sector factor and for V1.6 sector rotation research. Not traded in V1.5.
- **Tier 3 (large caps, 15 in V1.5, 30-50 with Plus):** the 15 US-listed common stocks with the highest 60-day median dollar volume that also satisfy: price ≥ $20, `fractionable` and `tradable` and `easy_to_borrow` on Alpaca, 60-day median quoted spread ≤ 5 bps, no earnings inside the next 5 sessions at entry time (from a static quarterly calendar the operator maintains in `config/earnings.csv` until a data source is added). M3 trades these in paper.

Total 30 symbols, matching the Basic-plan stream cap. Survivorship: the Tier 3 list for backtests is rebuilt at each quarter-start from point-in-time dollar volume, and names that were later delisted are kept in the historical universe if they were eligible then. Corporate actions: split-adjusted bars; positions through a split are reconciled from the broker.

### 9.2 Data frequency and cadence

- Streaming: 1-minute bars (IEX) for all 30 symbols, aggregated locally into 5-, 15-, and 30-minute bars on exact ET boundaries; quotes for symbols with a live order or position.
- Historical: SIP 1-minute bars fetched with a ≥16-minute lag for warm-up, volume features and backtests; SIP daily bars for M1 and M3.
- Decision cadence: every completed 30-minute bar (09:30, 10:00, ..., 15:30) plus two scheduled events (09:00 pre-open for OPG submissions; 15:58 for fractional M2 exits). The 30-minute cadence is chosen because M2 needs 10:00 and 15:30, M1 and M3 need only the daily close, and nothing in the evidence needs finer granularity.

### 9.3 Module M1: vol-managed index trend

- Inputs (daily close): SPY and QQQ 12-month return excluding the last 21 days (TSMOM sign); 21-day and 63-day annualised realised volatility σ21, σ63; σ = max(σ21, σ63).
- Target weight per ETF: w = clip(σ_target / σ, 0, w_max) × 1[TSMOM > 0], with σ_target = 10% annualised and w_max = 0.60. Two ETFs, so gross M1 exposure ≤ 120% by formula but capped by the allocator to the policy's gross limit (100% in V1.5).
- Rebalance: weekly on Monday's close (CLS for whole shares, market at 15:55 for fractional), or intraweek if |w_target − w_actual| > 0.10.
- Exit: w_target = 0 when TSMOM turns negative; no ATR stop. The catastrophe stop is the portfolio drawdown kill switch.
- Horizon: weeks to months. Turnover ≈ 2-4x per year per ETF.
- Evidence: [S15, S4, S1, S2]. Benchmark: buy-and-hold SPY and the existing `ma_crossover`.

### 9.4 Module M2: market intraday momentum

- Inputs: r1 = ln(P10:00 / Pprev_close) using the 09:30-10:00 30-minute bar close; r12 = ln(P15:30 / P15:00); σ_last = 21-day standard deviation of last-half-hour returns; relvol = (volume 09:30-15:15 today) / (21-day median of the same window), from lagged SIP bars.
- Signal at 15:30: s = sign(r1) if |r1| > k·σ_last, else 0, with k = 0.5 as the research default and the surface in [0.25, 1.0]. Research variant: s = sign(r1) only when sign(r1) = sign(r12) (agreement filter); relvol > 1 as an optional filter. Both variants are reported; the production choice is made by the protocol in section 11, not here.
- Position: direction s, sized by the allocator to a per-trade risk of σ_last × notional = 0.25% of equity (i.e. notional = 0.0025 E / σ_last), capped at 40% of equity per ETF. Long-only in live V1.5 (s = −1 becomes flat); both directions in paper.
- Entry: marketable limit at 15:30:30, limit = ask + 1 tick (buy), cancelled and re-priced once if unfilled after 20 seconds, abandoned if unfilled by 15:33.
- Exit: CLS (market-on-close) submitted immediately after the entry fills, for whole-share positions; for fractional positions a DAY market order at 15:58:00. Hard stop: none (the position lives 30 minutes); a software stop at 3·σ_last from entry is checked on 1-minute bars and, if hit, exits with a market order, purely as a fat-tail guard.
- Horizon: 30 minutes, one trade per ETF per day, roughly 250 per ETF per year, fewer with the threshold.
- Evidence: [S12, S13]. Benchmark: zero, and the unconditional last-half-hour return.
- Live gating: PDT status must be resolved (section 6) before M2 is enabled in live.

### 9.5 Module M3: large-cap residual reversal (paper-only in V1.5)

- Inputs (daily close): for each Tier 3 name, β from a 60-day regression on SPY; residual 5-day return res5 = r5 − β·r5(SPY); 21-day residual volatility σ_res; latest quoted spread; earnings exclusion flag.
- Signal at the close: rank res5 / σ_res across eligible names; candidates are the bottom 3 (most negative), subject to spread ≤ 5 bps and no earnings within 5 sessions.
- Entry: next opening auction (OPG market for whole shares; DAY limit at prior close for fractional, cancelled at 09:45 if unfilled).
- Exit: when res5 recomputed daily crosses above 0, or after 5 sessions, whichever first; CLS or 15:55 market. No ATR stop. Fat-tail guard: exit if the name falls more than 3·σ_res from entry on a close.
- Position: notional = 0.25% E / σ_res, capped at 15% of equity per name, at most 3 concurrent M3 positions in V1.5.
- Horizon: 1-5 days. Turnover ≈ 30-60 round trips per year at three slots.
- Evidence: [S7, S8, S9, S14]. Benchmark: equal-weight Tier 3 buy-and-hold, and the existing `mean_reversion`.
- Promotion condition: the section 11 protocol, including a positive net expectancy with a lower 90% confidence bound above zero on the untouched holdout, and paper slippage within 3 bps of the backtest assumption.

### 9.6 Portfolio construction (allocator)

Inputs: the set of TradeIntents this cycle plus open positions. Output: target positions.

1. Convert each intent to a risk budget: notional_i = r_i × E / σ_i where r_i is the module's per-trade risk (0.25% for M2 and M3; M1 uses its own target-vol formula).
2. Apply caps in order: per-symbol notional ≤ symbol_cap(E); per-module gross ≤ module_cap(E); sector concentration ≤ 30% of gross (Tier 3 names mapped to GICS sectors in config); portfolio gross ≤ gross_cap(E); portfolio net within [net_min, net_max]. When a cap binds, scale the newest intents pro rata, never the existing positions (which are reduced only by their own module's exit or by risk).
3. Correlation haircut: if the average pairwise 21-day correlation of the intended positions' residual returns exceeds 0.5, scale total new notional by 0.7. This is a transparent rule, not an optimiser.
4. Conflict: two modules on one symbol net to a single target; the net is what is sent; the position ledger tracks each module's slice.

No optimisation, no Kelly, no signal-strength weighting beyond the vol normalisation. Sophistication is added only when the research shows it changes outcomes.

### 9.7 Risk rules (unchanged principle: risk sits below strategy)

`RiskPolicy` (operator-owned config, hashed and displayed at arm time, cannot be changed by any process):
- allowed_modules, allowed_symbols, max_order_notional, max_symbol_exposure_pct, max_module_gross_pct, max_gross_pct, max_net_pct, max_daily_loss_pct, max_drawdown_pct, max_open_positions, allow_short, allow_margin, allow_overnight (per module), allow_extended_hours, max_spread_bps, max_stale_seconds, pdt_mode.
- V1.5 live defaults: modules {M1, M2 when PDT resolved}, gross 100%, net 0-100%, short off, margin off, overnight allowed for M1 only, daily loss 2%, drawdown 12%, 6 positions.
- V1.5 paper defaults: modules {M1, M2, M3}, gross 150%, net −50 to +150%, short on, margin on.

Stops: strategy-defined invalidation and time stops are the primary exits. Broker-resident protection exists as a catastrophe layer: OTO stop-loss legs at 4·σ_horizon for whole-share overnight positions (M1, M3); daily-replaced DAY stop orders for fractional overnight positions; none for M2 beyond the software guard. The 2×ATR(14) stop is retired from the default policy and kept as an option for the legacy baselines.

### 9.8 Shorting, overnight, market hours, execution, turnover

- Shorting: off in live V1.5; on in paper for M2 (short leg) and M3 (short losers-of-the-winners variant, research only). Requires $2,000 and ETB.
- Overnight: M1 and M3 hold overnight (M3's profit accrues overnight per [S14]); M2 never does.
- Extended hours: never in V1.5.
- Execution: marketable limits at scheduled times; OPG and CLS for whole-share auction orders; market orders only for fractional exits and the fat-tail guard; every order carries a deterministic client id `<run>-<module>-<sym>-<session>-<seq>-<kind>`.
- Expected turnover: M1 2-4x/yr per ETF; M2 up to 250 round trips/yr per ETF at 20-40% of equity each (≈ 100x/yr notional turnover, which is why its cost must be near 1 bp); M3 30-60 round trips/yr.

---

## 10. MATHEMATICAL RATIONALE

All numbers below were computed, not quoted; the script is reproducible from the formulas.

### 10.1 Expected P&L and the cost hurdle

E[PnL per trade] = p·E[win] − (1−p)·E[loss] − spread − slippage − fees − adverse selection − financing.

For a symmetric bet of size σ_h (the standard deviation of the return over holding horizon h) with round-trip cost c, breakeven requires p > 0.5 + c/(2σ_h). With SPY at 1% daily volatility:

| Horizon | σ_h (bps) | Breakeven p, c = 2 bps | c = 6 bps | c = 15 bps | IC needed for +2 bps net at c = 2 |
|---|---|---|---|---|---|
| 1 min | 5.1 | 0.70 | impossible | impossible | 0.79 |
| 5 min | 11.3 | 0.59 | 0.77 | impossible | 0.35 |
| 15 min | 19.6 | 0.55 | 0.65 | 0.88 | 0.20 |
| 30 min | 27.7 | 0.54 | 0.61 | 0.77 | 0.14 |
| 60 min | 39.2 | 0.53 | 0.58 | 0.69 | 0.10 |
| 2 h | 55.5 | 0.52 | 0.55 | 0.64 | 0.07 |
| 1 day | 100 | 0.51 | 0.53 | 0.58 | 0.04 |
| 5 days | 224 | 0.50 | 0.51 | 0.53 | 0.02 |
| 20 days | 447 | 0.50 | 0.51 | 0.52 | 0.01 |

The last column is the fraction of the horizon's standard deviation the signal must capture (an information coefficient) to net 2 bps against 2 bps of cost. Published intraday effects have IC-equivalents in the 0.05-0.15 range (Gao et al.'s R² of 1.6% corresponds to a correlation of about 0.13). That places the feasible frontier at 30 minutes and beyond for the cheapest instruments, and at hours to days for single stocks with 6-15 bps costs. This proves the brief's suspicion: **5 minutes is not feasible for us; 30 minutes to multi-day is.**

Worked example from the brief: if round-trip implementation shortfall is 6 bps, a strategy needs at least 6 bps of gross expectation to break even, and on a 2-hour horizon (σ ≈ 55 bps) that is an IC of 0.11, or a 55.4% win rate on symmetric payoffs. Doubling costs to 12 bps pushes the required win rate to 61%, which no documented intraday equity effect delivers. Cost stress is therefore the first robustness test, not the last.

### 10.2 Sample size

To reject zero mean at t = 2 with per-trade mean μ and standard deviation σ you need N = (2σ/μ)² trades:

| μ (bps) | σ (bps) | N |
|---|---|---|
| 1 | 11 (5-min) | 484 |
| 3 | 28 (30-min, M2) | 348 |
| 3 | 55 (2-h) | 1,344 |
| 5 | 55 | 484 |
| 10 | 100 (daily) | 400 |
| 20 | 100 | 100 |
| 30 | 224 (M3, 5-day) | 215 |

M2 at a realistic 2.5 bps net and 28 bps per-trade dispersion needs about 500 trades, two calendar years at one trade per day, before a t-statistic of 2 is even possible. M3 at 30 bps net needs about 215 trades. The current MA baseline's 10-15 out-of-sample trades cannot distinguish a Sharpe of 0.6 from zero, and no amount of walk-forward stitching fixes that.

### 10.3 Sharpe uncertainty and the deflated Sharpe

The standard error of an annualised Sharpe estimated from daily returns is approximately 1/√years (Lo 2002; Bailey and López de Prado 2012 [S31]): 2.0 after one quarter, 1.0 after one year, 0.5 after four years, 0.35 after eight. "Sharpe 0.65 on the walk-forward" is therefore "0.65 ± 1.0".

The expected maximum Sharpe across N independent trials with no skill, following the deflated Sharpe ratio construction [S31], is:

| Trials N | 3-year sample | 8-year sample |
|---|---|---|
| 5 | 0.69 | 0.42 |
| 20 | 1.10 | 0.67 |
| 50 | 1.31 | 0.80 |
| 200 | 1.60 | 0.98 |
| 1,000 | 1.88 | 1.15 |

A parameter surface with 50 points evaluated on three years of minute data will produce a best-point Sharpe near 1.3 from noise alone. The research protocol (section 11) therefore requires that every reported Sharpe be accompanied by the number of trials and the deflated value, and that the median of the surface, not its maximum, be the number promoted.

### 10.4 Compounding, volatility drag, and Kelly

Geometric growth g ≈ μ − σ²/2. At 15% volatility the drag is 1.1% per year; at 50% (which 3x leverage on QQQ produces) it is 12.5% per year, which is why leverage "merely magnifies noise" for a low-Sharpe strategy. The full Kelly fraction f* = μ/σ² for μ = 8%, σ = 15% is 3.6x, but with five years of history the standard error of μ is 6.7%, so the 95% range of the estimate runs from −2.4x to +9.5x. Estimated Kelly is not a sizing rule; it is a random number with the right sign. V1.5 sizes to a risk budget (0.25% of equity per trade, 10% target volatility for M1) and never above 1x gross in live.

### 10.5 Correlation between simultaneous trades

Effective number of independent bets N_eff = N / (1 + (N−1)ρ):

| ρ | N = 5 | N = 20 | N = 50 |
|---|---|---|---|
| 0.2 | 2.8 | 4.2 | 4.6 |
| 0.4 | 1.9 | 2.3 | 2.4 |
| 0.6 | 1.5 | 1.6 | 1.6 |
| 0.8 | 1.2 | 1.2 | 1.2 |

Twenty large-cap longs with the typical 0.5-0.7 intraday correlation to the market are about 1.6 bets, not 20. This is the quantitative reason M3 is built on market-residual returns, why the allocator applies a correlation haircut, and why "more symbols" is not the same as "more opportunities". Diversification in V1.5 comes from modules with different mechanisms and horizons, not from symbol count.

### 10.6 The 2×ATR stop

For a 200-day trend rule holding ~100 sessions in a 1%/day instrument, the probability that a barrier k% below entry is touched by a daily close (reflection principle, zero drift) is:

| Stop | P(hit within 100 sessions) |
|---|---|
| 2×ATR ≈ 2% | 0.84 |
| 3×ATR ≈ 3% | 0.76 |
| 4×ATR ≈ 4% | 0.69 |
| 6×ATR ≈ 6% | 0.55 |

A 2% stop is touched five times out of six on a multi-month hold. That matches the repo's finding that 16 of 26 exits were stops and explains the stop-then-re-enter churn. The stop's distance must scale with √(holding horizon), which is what V1.5's 4·σ_horizon catastrophe stop does (for M1, σ_horizon over a week is about 2.2%, so the stop sits near 9%, and it is a backstop, not the exit).

### 10.7 M2 economics, end to end

Gross 4 bps per trade (a deliberately haircut version of the published effect), cost 1.5 bps, net 2.5 bps, per-trade dispersion 28 bps: per-trade Sharpe 0.09, annualised over 250 trades ≈ 1.4 before any threshold or PDT losses; 500 trades to reach t = 2. If the effect has decayed to 2 bps gross, the net is 0.5 bps and the strategy is a coin flip after costs; that is exactly the scenario the cost-stress tests in section 11 are designed to reveal before capital is committed.

### 10.8 Drawdown and sequence risk

With a 12% drawdown kill switch and a portfolio running at 10% annualised volatility, a 12% peak-to-trough is roughly a 1.2σ annual event with positive drift and happens about once per decade in normal conditions, more often in 2008/2020-type regimes. The daily loss halt at 2% is a 1.6σ daily event at 10% vol (~5% of days if the portfolio were fully invested every day; far fewer given M2's 30-minute exposure). These are set so that normal operation rarely trips them and a broken module trips them within days.

---

## 11. RESEARCH / BACKTEST PLAN

### 11.1 Data partitions (fixed once, recorded in `research/PROTOCOL.md`)

Alpaca history starts in 2016. Minute bars 2016-2026 give ten years for M2; daily bars for M1 and M3.

- Dataset A (exploration): 2016-01 to 2018-12. Anything goes, nothing reported.
- Dataset B (parameter development): 2019-01 to 2021-12. Parameter surfaces built here.
- Dataset C (validation): 2022-01 to 2023-12. One look per candidate; failing candidates return to A/B and are recorded as extra trials.
- Dataset D (untouched holdout): 2024-01 to 2026-06. Opened once, at the end, for the promoted configuration only; the run is logged with a hash of the code and config.
- Rolling walk-forward across B+C+D with 2-year train / 6-month test, parameters chosen by median-of-surface, is reported alongside.

### 11.2 Backtester requirements (what must change)

The daily engine cannot represent V1.5. The new engine must:
- Process 1-minute bars per symbol with an exact ET session calendar (regular hours, early closes at 13:00, holidays), aggregate to 30-minute decision bars, and expose auction events (open, close) as distinct fill points.
- Fill model: marketable limit at bar-open ± half spread ± slippage, with the spread estimated from lagged SIP quotes (or from a per-symbol schedule when quotes are unavailable); OPG fills at the session open price with an opening-auction slippage parameter; CLS fills at the session close with a closing-auction parameter; market orders fill at the next 1-minute bar's open with volume-based partial fills when order notional exceeds 1% of that bar's SIP dollar volume; stop orders elect on the first 1-minute bar whose low (high) touches the stop and fill at the next bar open.
- Portfolio-level concurrency: multiple modules, one ledger, allocator and risk engine in the loop, order priority (risk exits, then module exits, then entries).
- Timestamps on every order and fill; no fill may use information from its own or later bars.
- Conservative by default: it must not pretend to know tick-level fills. Spread and slippage parameters are explicit and stress-tested.

### 11.3 Tests that must pass before anything goes to paper

1. Parameter surface on B: median Sharpe > 0, ≥ 70% of neighbours within 30% of the best point, no single point more than 2x the median.
2. Validation on C: net expectancy > 0 with a bootstrap lower 90% bound > 0 (block bootstrap by day for M2, by week for M3).
3. Cost stress on C: survives 2x spread, 2x slippage, a one-bar execution delay, 10% of signals randomly dropped, and a random 2 bps adverse fill on 20% of trades. "Survives" means the point estimate stays positive and drops by no more than 60%.
4. Regime table on C: no regime (trend/range × low/high vol) with a Sharpe below −0.5 that accounts for more than 25% of the sample.
5. Multiple testing: reported deflated Sharpe > 0 given the logged trial count.
6. Overnight/intraday attribution: M3's profit must accrue overnight and M2's intraday, consistent with the literature; a module whose profit shows up in the wrong window is suspect.
7. Holdout D: opened once; must not contradict C (Sharpe within one standard error).

### 11.4 Paper gates (before live)

- ≥ 60 sessions of paper running the full autonomous loop with zero unreconciled discrepancies, zero orphaned orders, zero duplicate orders.
- Realised paper slippage vs reference within 3 bps of the backtest assumption (accepting that paper fills flatter).
- Every watchdog action exercised at least once in paper (kill, halt, flatten-on-policy).
- M2 in paper: ≥ 120 trades with net expectancy of the same sign as the backtest.

### 11.5 Live gates

- All existing interlock gates; PDT regime verified; account funded to the level the policy assumes.
- First 20 sessions at safe-mode caps (as today), then step-ups of at most 2x per month, each requiring a fresh review report.
- Live slippage log compared weekly to paper and backtest; a 5 bps degradation halts entries pending review.

---

## 12. SYSTEM ARCHITECTURE V1.5

```
                        +----------------------+
                        |  RiskPolicy (config) |  operator-owned, hashed, displayed at arm
                        +----------+-----------+
                                   |
  Alpaca WS (IEX 1m bars, quotes)  |     Alpaca WS trade_updates
        |                          |               |
        v                          v               v
+-------+--------+     +-----------+------+   +----+-----------------+
| MarketDataHub  |     | RiskEngine        |   | OrderManager          |
| reconnect,     |     | (RiskManager.     |   | idempotent ids, OMS   |
| dedupe, gap    |     |  check_order +    |   | state machine, OTO/   |
| detect, 30m    |     |  exposure ledger  |   | bracket, replace,     |
| aggregation,   |     |  + policy caps)   |   | cancel, reconcile     |
| SIP backfill   |     +-----------+------+   +----+-----------------+
+-------+--------+                 ^               ^
        v                          |               |
+-------+--------+   TradeIntents  |   Orders      |
| FeatureEngine  +------> +--------+-------+ ------+
| per-symbol     |        | Allocator      |
| rolling feats  |        | vol-normalise, |
+-------+--------+        | caps, haircut  |
        v                 +----------------+
+-------+--------+                 ^
| Strategies     |                 |
| M1 M2 M3 +     +-----------------+
| ma_crossover   |
| mean_reversion |
+----------------+
        |
+-------v--------+   +------------------+   +---------------------+
| StateStore     |   | Watchdog (sep.   |   | Reporter / Alerts    |
| (SQLite WAL)   |   | process)         |   | Discord, daily md    |
+----------------+   +------------------+   +---------------------+
```

Module descriptions:
- **MarketDataHub**: owns the single WebSocket connection (Basic cap), authenticates, subscribes, reconnects with backoff and jitter, deduplicates by (symbol, timestamp), detects missing minutes and backfills from REST, aggregates to 30-minute bars on ET boundaries, publishes `BarEvent`s. Also runs the lagged SIP fetcher for volume features.
- **FeatureEngine**: incremental per-symbol features (O(1) per bar, in the style of `indicators.py`): realised vols at several horizons, r1/r12, VWAP, β and residual returns vs SPY, spread estimate from the latest quote, time-of-day normalisation.
- **Strategies**: implement `on_event(FeatureSnapshot, PositionView) -> list[TradeIntent]`. The legacy `Strategy.on_bar` is wrapped by an adapter that turns `Signal(target)` into a TradeIntent with `expected_edge_bps=None`.
- **TradeIntent**: `symbol, module_id, ts, direction, horizon_seconds, reference_price, expected_edge_bps (nullable), signal_strength [0,1], volatility (σ_horizon), risk_budget_pct, invalidation_price (nullable), max_holding_seconds, entry_style (marketable_limit | opg | cls | market), exit_style, overnight_ok, tag`.
- **Allocator**: section 9.6.
- **RiskEngine**: the existing `RiskManager` extended with an exposure ledger (gross, net, per-module, per-symbol, per-sector), the `RiskPolicy` caps, PDT accounting, and a broker-protection requirement check (a position that must have a broker-side stop cannot be opened without one).
- **OrderManager**: order state machine driven by `trade_updates` with polling fallback; supports market, limit, stop, OTO/bracket, replace, cancel; enforces one in-flight order per (symbol, module); deterministic client ids.
- **StateStore**: SQLite in WAL mode replacing the JSON blob: tables for orders, fills, positions (per module slice), risk state, decisions, heartbeats. Atomic transactions around submit.
- **Watchdog**: section 13.
- **Reporter**: daily markdown report, decision log, slippage log, the existing dashboard.

Backward compatibility: `python -m bot backtest --strategy ma_crossover` keeps working through the adapter; the daily engine remains for the two baselines until the new engine reproduces its numbers within rounding.

---

## 13. AUTONOMOUS RUNTIME DESIGN

Operator authorises a configuration (RiskPolicy + module set + universe), arms it (existing typed-phrase flow, extended with the policy hash), and the daemon runs:

1. **Boot**: load config, validate policy hash against the armed hash, open StateStore, verify environment (host, key prefix, account shape), write heartbeat.
2. **Reconcile**: fetch orders (all open, plus all since the last watermark), positions, account; replay `trade_updates` gap if any; adopt broker positions into the ledger with `module_id=orphan` and attach a protective stop per policy; cancel any order not in the ledger (orphan) unless it is a protective leg.
3. **Subscribe**: market data and trade updates; wait for the first complete bar; mark data fresh.
4. **Schedule**: an ET-aware scheduler emits events: pre-open (09:00), each 30-minute bar close, 15:30 M2 window, 15:50 CLS cutoff, 15:58 fractional exits, close, post-close reporting (16:30), nightly maintenance (19:30: SIP backfill, universe checks, protective-order re-placement plan for the next day).
5. **Cycle** (per event): features → strategies → intents → allocator → risk → orders; every step logged as a decision record.
6. **Monitor**: fills arrive via stream; positions update; protective legs confirmed; slippage logged.
7. **Exit**: module exits, time stops, invalidations, policy-driven flatten.
8. **Report**: daily markdown and alerts.
9. **Restart**: systemd restarts the process on crash; boot resumes at step 1; the ledger and idempotent ids make a restart during submission safe (existing property, preserved).

Human approval points: deployment of a new config or policy (arm), kill-switch reset, universe changes, module promotion. Not per trade.

Deployment recommendation: a small Linux VPS (2 vCPU, 4 GB, in a US-East region to keep latency to Alpaca low, on the order of $12-25/month) running Ubuntu 24 with two systemd services (`tbot.service`, `tbot-watchdog.service`), the repo deployed by `git pull` plus a tagged release checkout, secrets in a root-owned 600 env file loaded via `EnvironmentFile` (or systemd `LoadCredential`), logs to journald and a rotated JSONL file, time from chrony. Docker is optional and adds little for a single Python process; a Mac is rejected because sleep, updates and Wi-Fi make "always-on" a fiction. AWS/GCP is fine but overkill; a $12 VPS with a snapshot backup is the right size for this bot's requirements.

---

## 14. RISK MODEL

Hard constraints, all in `RiskPolicy`, all enforced in `RiskEngine`, none reachable from strategy, allocator, advisor, or watchdog code (enforced by the existing import-scan architecture tests, extended).

| Constraint | Live V1.5 default | Paper default | Enforced where |
|---|---|---|---|
| Allowed modules | M1, M2 (after PDT check) | M1, M2, M3 | intent admission |
| Allowed symbols | the 30-symbol universe | same | intent admission and `check_order` |
| Max order notional | 40% of equity (safe mode: $25) | 40% | `check_order` |
| Max symbol exposure | 60% of equity | 60% | ledger |
| Max module gross | M1 100%, M2 40%, M3 45% | M1 120%, M2 80%, M3 60% | ledger |
| Max portfolio gross | 100% | 150% | ledger |
| Net exposure | 0% to 100% | −50% to 150% | ledger |
| Daily loss halt | 2% of day-start equity (safe: $5) | 3% | `update_equity` |
| Drawdown kill | 12% from peak (safe: $10) | 20% | `update_equity` |
| Max open positions | 6 | 12 | `check_order` |
| Shorting | off | on (needs $2k) | `check_order` |
| Margin | off | on | buying-power check uses cash, not buying power |
| Overnight | M1 yes, M2 no, M3 yes | same | intent admission and end-of-day flatten |
| Extended hours | off | off | `check_order` |
| Max spread | 5 bps ETFs, 10 bps stocks | same | `check_order` |
| Max staleness | 90 s during regular hours for intraday intents; 900 s for auction orders | same | `check_order` |
| PDT mode | `legacy_guard` (block any intent that would create a 4th day trade in 5 sessions) or `intraday_margin` (no guard) as verified from the account | n/a | intent admission |
| Broker protection | required for overnight whole-share positions; daily stop required for fractional overnight positions | same | position open and nightly check |

Kill-switch behaviour: cancel all non-protective orders, close all positions at market (CLS if before 15:50 on a whole-share position, otherwise market), halt, alert, require human reset. Unchanged from today.

---

## 15. FAILURE MODES

**Technical**
- WebSocket silently stalls (no frames, no error): detected by the watchdog's data-freshness check; entries halt after 90 s without a bar during regular hours.
- Reconnect storm hits the 406 connection limit: backoff with jitter capped at 5 minutes; second connection attempts are refused by design.
- Duplicate or out-of-order bars after reconnect: deduplicated by (symbol, timestamp); a bar older than the last processed is logged and dropped.
- Missing minute bars (IEX has no print in a minute for thin names): forward-filled for features with a flag; an intent is not admitted if more than 20% of the last 30 minutes are synthetic.
- Trade update lost during a disconnect: reconciliation on reconnect polls orders since the watermark.
- Restart mid-submission: existing idempotent-id design; the ledger row is written as `submitting` before the call and resolved on boot.
- Restart with a position and no protective order: boot attaches one per policy or, if the policy forbids the position (e.g. an orphan short), flattens it.
- Broker 5xx / 429: retries with backoff (existing); after the retry budget, entries halt, exits keep retrying.
- Clock skew: the scheduler uses the broker clock endpoint at boot and every hour; a skew above 2 s halts intraday entries.
- Early close / holiday: session calendar from the broker; the scheduler compresses the day (CLS cutoff 12:50 on a 13:00 close).
- Fractional rounding leaves a dust position: nightly job closes any position under $1 notional.
- Bracket or OTO rejection (stop too close to base price): the risk engine widens to the minimum $0.01 offset and, if still rejected, falls back to a software stop and marks the position `unprotected`, which the watchdog reports.

**Financial**
- M2 effect decayed: shows up first as net expectancy near zero with normal dispersion; the weekly review flags 60 consecutive trades with a negative running mean; entries are throttled to half size automatically and the operator is alerted (throttling down is the one automatic risk change permitted, tightening only).
- Momentum crash hitting M3's longs: sector and gross caps limit damage; the 3·σ_res fat-tail exit per name; the daily halt.
- Overnight gap through M1: position sizing at 10% target vol bounds it; no stop can help.
- Correlation spike (everything becomes one bet): the allocator's correlation haircut and the gross cap.
- Fill quality worse than assumed: the slippage log and the 5 bps degradation halt.
- Paper-to-live divergence: safe-mode caps for the first 20 sessions.
- Operator error (wrong policy, wrong env): policy hash at arm time, env checks at boot, the existing interlock.

---

## 16. IMPLEMENTATION ROADMAP

Each phase ends with a commit tag and a review report. Nothing goes to paper before Phase 4, nothing to live before Phase 7.

- **Phase 0 (audit, 1-2 days).** Claude Code re-audits the repo, runs tests, records the baseline numbers on real SPY/QQQ, and writes `V1_5_AUDIT.md`. No code changes.
- **Phase 1 (data, 3-5 days).** Minute-bar provider (SIP lagged, IEX), DuckDB schema for minute bars and quotes, session calendar with early closes, 30-minute aggregation, quality checks for minute data. Tests: gaps, duplicates, early close, holiday, lagged-SIP boundary.
- **Phase 2 (backtester, 5-8 days).** New engine per section 11.2; fill models; multi-module ledger; reproduces the daily engine's baseline numbers within rounding when fed daily bars. Tests: no-lookahead by future mutation, auction fills, stop election, partial fills, cost stress hooks.
- **Phase 3 (research, 1-3 weeks, mostly compute and reading).** Implement M1, M2, M3 as research candidates; run the section 11 protocol on A/B/C; write `research/RESULTS_V1_5.md` with surfaces, deflated Sharpes, cost stress, attribution. Holdout D stays sealed.
- **Phase 4 (spine, 5-8 days).** TradeIntent, allocator, RiskPolicy/RiskEngine, OrderManager with OTO/bracket/replace, SQLite StateStore with migration from JSON, adapters for the two baselines. Tests: the Part 32 list.
- **Phase 5 (streaming and runtime, 5-8 days).** MarketDataHub, trade_updates consumer, scheduler, daemon, watchdog, systemd units, deployment doc. Tests: reconnect, duplicate events, stale data, restart matrix.
- **Phase 6 (paper, ≥ 60 sessions).** Full autonomous paper run with M1, M2, M3; weekly review reports; holdout D opened once at the end for the promoted configuration.
- **Phase 7 (live, staged).** M1 only at safe-mode caps for 20 sessions; then M2 if and only if the PDT regime permits; step-ups per section 11.5.
- **Phase 8 (V1.6 planning).** Shorts in live after funding to $2k, Algo Trader Plus, Tier 3 expansion to 50 names, sector rotation research.

---

## 17. WHAT SHOULD NOT CHANGE

- The principle that risk sits below strategy, and `RiskManager.check_order` as the final deterministic gate.
- The paper/live credential split, key-prefix and host checks, `TRADING_ENV`, `--live`, arm/disarm with TTL and fingerprint, `LIVE_AUTONOMOUS_TRADING=false` default, `SAFE_LIVE_TEST_MODE` caps.
- Deterministic client order ids and state-written-before-and-after-submission.
- Broker is authoritative; local state reconciles to it.
- Incremental O(1) indicators and the event-driven strategy discipline.
- The research-cannot-order and advisor-shadow-only architecture tests.
- Log redaction, decision records, Discord alerts, the review command.
- The two baselines, as benchmarks.
- The documentation habit (AUDIT, DATA, STRATEGY_SPEC, RUNBOOK); V1.5 adds to it rather than replacing it.

---

## 18. WHAT SHOULD BE REPLACED

- `Signal(target ∈ {−1,0,1})` as the only strategy output: replaced by TradeIntent (legacy adapter kept).
- Single-strategy `Trader` with per-session replay: replaced by the event-driven daemon; replay-from-history stays as the warm-up mechanism.
- Daily-only data layer and backtester: extended to minute bars and auctions.
- JSON state blob: replaced by SQLite (the JSON store remains readable for migration).
- Poll-only order tracking: replaced by `trade_updates` with polling fallback.
- `submit_market_order` as the only broker method: extended with limit, stop, OTO/bracket, replace, cancel-by-id.
- 2×ATR(14) as the default stop: retired from the default policy; horizon-scaled catastrophe stops and strategy invalidations instead.
- `MAX_POSITION_PCT` as the only exposure control: replaced by the RiskPolicy ledger caps.
- Full-sample regime labels in the harness: replaced by causal labels.
- Hand-picked walk-forward grids: replaced by declared surfaces with trial counting.

---

## 19. WHAT COULD I BE WRONG ABOUT?

- **That market intraday momentum is still there in 2024-2026.** Both JFE papers end in 2013 and 2020. It is a published, well-known effect, and post-publication decay is the base case. The design treats M2's live enablement as conditional on the holdout and on paper results, and the M2 economics in section 10.7 show how thin the margin is. If it has decayed, V1.5 becomes M1 plus M3, which is still a better platform than V1.
- **That IEX-only data is good enough for M2.** IEX prices track the NBBO closely for SPY and QQQ, but the 10:00 and 15:30 bar closes could differ from SIP by a tick or two, and that is comparable to the edge. Phase 3 must measure the IEX-vs-SIP difference on lagged data before trusting it.
- **That the PDT problem has a solution short of $25k.** If Alpaca keeps the legacy rule for small accounts through the transition, M2 in live waits, possibly for a year. That is a real cost of the recommendation and I have not hidden it.
- **That M3 survives costs at all.** The de Groot et al. result is the only direct evidence of net profitability, on a much larger universe with smarter portfolio construction than three long slots. M3 is paper-only for that reason and I would not be surprised if it fails promotion.
- **That vol-managed trend improves on buy-and-hold.** Cederburg et al. show the mean-variance benefit is fragile out of sample; the robust benefit is drawdown reduction, and an investor with a long horizon and no leverage constraint could reasonably prefer buy-and-hold. M1 is defended as the beta sleeve of a system that needs its drawdowns bounded so the other modules can run, not as alpha.
- **That three modules diversify.** Intraday momentum and short-term reversal are conceptually opposite, but in a crash both can lose on the same afternoon. The correlation haircut and gross cap are the mitigation; the paper period is where the assumption is tested.
- **That a 30-minute cadence is fine.** Some of the M2 effect may be concentrated in the last 10-15 minutes and a 15:30 entry may miss part of it or, worse, front-run a reversal at 15:45. The surface over entry time (15:15, 15:30, 15:45) in Phase 3 decides.
- **That the current fake broker resembles Alpaca.** Same caveat as FINAL_REPORT: the smoke test's fill/restart/reconcile legs still need a market-hours run.
- **That I have the current SPY numbers right.** They came from your message, not from the repo.

---

## 20. RECOMMENDATION

Build V1.5 as a multi-strategy platform, not a new strategy. Keep the spine that already works (risk gate, idempotent execution, credential separation, research discipline) and replace the parts that cannot express anything beyond a daily long/flat signal (strategy interface, data layer, backtester, order manager, state store, runtime).

What it will trade and why:

- **SPY and QQQ, weekly, sized to 10% volatility with a 12-month sign filter (M1).** Because the only robust part of trend-following on a single equity index is the volatility management, and because a system that runs intraday and multi-day modules needs a bounded-drawdown beta sleeve as its base.
- **SPY and QQQ, once a day for the last 30 minutes, in the direction of the first half-hour when that move is large relative to recent last-half-hour volatility (M2).** Because it is documented in two JFE papers with an economic mechanism (end-of-day hedging flows), because it trades the cheapest instruments on earth at the deepest part of the day, and because 250 trades a year is enough to know within two years whether it still works. Long-only in live, paper-only until the account's PDT regime allows daily round trips.
- **The three most oversold of 15 mega-cap stocks, bought at the open and held one to five days (M3), in paper only.** Because short-term reversal in the largest names is a documented return to liquidity provision that is either just above or just below the cost hurdle depending on implementation, and the only honest way to find out is the protocol.

Around them: a 30-symbol universe that fits the free data plan, a TradeIntent bus and volatility-normalised allocator, an operator-owned RiskPolicy that no process can change, horizon-scaled catastrophe stops instead of 2×ATR, broker-resident protection where Alpaca allows it, a systemd daemon with a separate watchdog on a small VPS, and a research protocol with a sealed holdout, cost stress, trial counting and deflated Sharpe ratios.

Expected behaviour, with ranges: 1-3 orders per day in live (M2 daily when the threshold is met, M1 a few times a month), 3-8 per day when M3 is promoted; average holding time 30 minutes for M2, 1-5 days for M3, weeks for M1; gross exposure 20-100% of equity in live; a realistic transaction-cost hurdle of 1-3 bps per round trip on the ETFs and 6-12 bps on the stocks; and no verdict on any module before roughly 500 M2 trades or 200 M3 trades.

The single most important sentence in this report: the 2×ATR stop and the 10-trade sample are what made V1 look like it was doing something. V1.5's job is to give the system enough independent, cheap, mechanistically grounded decisions that the data can tell you what is real.

---

## SOURCES

Tier A unless marked.

- [S1] Moskowitz, Ooi, Pedersen (2012). Time series momentum. Journal of Financial Economics 104(2), 228-250. https://doi.org/10.1016/j.jfineco.2011.11.003
- [S2] Kim, Tse, Wald (2016). Time series momentum and volatility scaling. Journal of Financial Markets 30, 103-124. https://www.sciencedirect.com/science/article/abs/pii/S1386418116301379
- [S3] Huang, Li, Wang, Zhou (2020). Time series momentum: Is it there? Journal of Financial Economics 135(3), 774-794. https://ideas.repec.org/a/eee/jfinec/v135y2020i3p774-794.html
- [S4] Hurst, Ooi, Pedersen (2017). A century of evidence on trend-following investing. Journal of Portfolio Management 44(1), 15-29.
- [S5] Novy-Marx, Velikov (2016). A taxonomy of anomalies and their trading costs. Review of Financial Studies 29(1), 104-147.
- [S6] Chen, Velikov (2023). Zeroing in on the expected returns of anomalies. Journal of Financial and Quantitative Analysis 58(3), 968-1004. https://doi.org/10.1017/S0022109022000874
- [S7] Nagel (2012). Evaporating liquidity. Review of Financial Studies 25(7), 2005-2039.
- [S8] Avramov, Chordia, Goyal (2006). Liquidity and autocorrelations in individual stock returns. Journal of Finance 61, 2365-2394.
- [S9] de Groot, Huij, Zhou (2012). Another look at trading costs and short-term reversal profits. Journal of Banking and Finance 36(2), 371-382. Working paper: https://www.efmaefm.org/0efmameetings/efma%20annual%20meetings/2011-Braga/papers/0259.pdf (Tier A published, cited as the survivability evidence)
- [S10] Heston, Korajczyk, Sadka (2010). Intraday patterns in the cross-section of stock returns. Journal of Finance 65(4), 1369-1407. https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2010.01573.x
- [S11] Zarattini, Aziz (2023). Can day trading really be profitable? (ORB). SSRN. Tier B.
- [S12] Gao, Han, Li, Zhou (2018). Market intraday momentum. Journal of Financial Economics 129(2), 394-414. https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351
- [S13] Baltussen, Da, Lammers, Martens (2021). Hedging demand and market intraday momentum. Journal of Financial Economics 142(1), 377-403. https://ideas.repec.org/a/eee/jfinec/v142y2021i1p377-403.html ; SSRN 3760365
- [S14] Lou, Polk, Skouras (2019). A tug of war: Overnight versus intraday expected returns. Journal of Financial Economics 134(1), 192-213. https://personal.lse.ac.uk/polk/research/TugOfWar.pdf
- [S15] Moreira, Muir (2017). Volatility-managed portfolios. Journal of Finance 72(4), 1611-1644.
- [S16] Ernst, Sokobin, Spatt (2022/2024). Price impact in closing auctions, opening auctions, and continuous markets. SSRN. Tier A working paper.
- [S17] McLean, Pontiff (2016). Does academic research destroy stock return predictability? Journal of Finance 71(1), 5-32.
- [S18] Zarattini, Aziz, Barbon (2024). Beat the market: An effective intraday momentum strategy for S&P500 ETF (SPY). Swiss Finance Institute Research Paper 24-97, SSRN 4824172. Tier B. https://ideas.repec.org/p/chf/rpseri/rp2497.html
- [S19] Independent TradingView reimplementation notes reporting smaller returns and lower Sharpe than [S18], and a corrected calculation error. Tier D, used only as a caution. https://www.tradingview.com/script/gJeM3LZ5-Out-of-the-Noise-Intraday-Strategy-with-VWAP-YuL
- [S20] Alpaca Support. Difference between paper and live trading. https://alpaca.markets/support/difference-paper-live-trading (Tier C)
- [S21] Alpaca Community Forum. Paper trading latency vs live. https://forum.alpaca.markets/t/massive-paper-trading-latency-vs-live-trading/9053 (Tier D, directional only)
- [S22] Alpaca Docs. Websocket Streaming (trade_updates). https://docs.alpaca.markets/docs/websocket-streaming
- [S23] Example of reconnect-plus-polling-fallback pattern in an open-source Alpaca client: https://pypi.org/project/hedger/0.1.1/ (Tier C)
- [S24] Alpaca rate-limit profile (Trading API 200/min; Basic stream one connection, limited symbols; 406 on over-connect). https://apis.io/rate-limits/alpaca-markets/alpaca-markets-rate-limits/ (Tier C, consistent with [S25])
- [S25] Alpaca Docs. About Market Data API: Basic vs Algo Trader Plus (IEX vs all exchanges; 30 symbols vs unlimited; since 2016; 15-minute SIP restriction; 200 vs 10,000 calls/min; $99/month). https://docs.alpaca.markets/us/docs/about-market-data-api
- [S26] Alpaca Docs. Placing Orders (order types, bracket/OCO/OTO, trailing stops, TIF tables, auction cutoffs, order handling standards). https://docs.alpaca.markets/us/docs/orders-at-alpaca
- [S27] Alpaca Docs. Fractional Trading (market/limit/stop/stop-limit, DAY only, no shorts, NBBO pricing, day-trade counting). https://docs.alpaca.markets/us/docs/fractional-trading ; https://alpaca.markets/support/what-is-fractional-trading
- [S28] Alpaca Docs. Margin and Short Selling (ETB only, HTB unsupported, $0 borrow on ETB, maintenance table). https://docs.alpaca.markets/us/docs/margin-and-short-selling
- [S29] Alpaca Support. $2,000 equity required for margin and shorting. https://alpaca.markets/support/determine-margin-account
- [S30] Alpaca Docs. Understanding FINRA's new intraday margin rule and the end of PDT (12-month transition, firms may apply either regime). https://docs.alpaca.markets/us/docs/understanding-finras-new-intraday-margin-rule-and-the-end-of-pdt
- [S31] Bailey, López de Prado (2014). The deflated Sharpe ratio. Journal of Portfolio Management 40(5), 94-107; and Lo (2002). The statistics of Sharpe ratios. Financial Analysts Journal 58(4).
- Additional references used without a bracket tag: Jegadeesh-Titman 1993 JF; Daniel-Moskowitz 2016 JFE (momentum crashes); Barroso-Santa-Clara 2015 JFE; Cederburg, O'Doherty, Wang, Yan 2020 JFE (on volatility management); Bogousslavsky 2021 JFE; Berkman et al. 2012 JFE; Gatev-Goetzmann-Rouwenhorst 2006 RFS; Do-Faff 2010 FAJ; Bernard-Thomas 1989; Martineau 2022 (PEAD in large caps); Cont-Kukanov-Stoikov 2014 J. Fin. Econometrics; Andersen-Bollerslev 1997; Harvey-Liu-Zhu 2016 RFS (multiple testing).
