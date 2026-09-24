# REPORT — what was built, what the backtests show, and whether anything deserves paper trading

Updated 2026-09-24 (numbers re-run after the engine's exit-precedence fix; changes vs the 2026-09-21 report are
within ±0.5% and noted where relevant). All numbers reproducible with the commands in each section.

## 1. What was built

See AUDIT.md (state before this round) and FINAL_REPORT.md (state after). In short: a no-lookahead daily-bar
backtester with costs and walk-forward; two baseline strategies; a shared deterministic risk layer with a pre-trade
gate; an execution loop with crash-safe state, idempotent orders, partial-fill handling and reconciliation; a
paper/live credential split with a multi-gate live interlock and dollar caps; a research harness; 126 offline tests
plus opt-in real-paper integration tests.

## 2. Data used, and the important caveat

The build environment has no outbound access to Alpaca, so no SPY/QQQ bars were available. Everything below runs on
real historical prices that ship inside two PyPI wheels (`scripts/load_sample_data.py`): the S&P 500 index and 20
large caps (close-only, 1990–2022) and GOOG (OHLCV, 2004–2013). Close-only data means open = high = low = close:
fills happen at the next *close* (a full extra day of delay), ATR uses close-to-close moves only, and gaps are
invisible. `SP500` is an index (no expense ratio, dividends, or tracking error). The five-stock basket is
survivorship-biased by construction. Costs: 2 bps slippage + 1 bp half-spread per side. Risk defaults: 1% risk per
trade against a 2×ATR stop, ≤50% of equity per position, no leverage, 3% daily halt, 20% kill switch. Because of
the sizing the strategies are typically 30–65% invested while buy-and-hold is 100%.

## 3. Results

### 3.1 `ma_crossover` on SP500, 2000-01-03 → 2022-12-28

`python -m bot backtest --strategy ma_crossover --symbol SP500 --start 2000-01-03 --end 2022-12-28`

| | Full period, fixed 50/200 | Walk-forward OOS (2003–2022) | Buy & hold (same OOS window) |
|---|---|---|---|
| Total return | +95.7% | +53.0% | +318.5% |
| CAGR | +2.96% | +2.15% | +7.43% |
| Sharpe | 0.51 | **0.55** | 0.47 |
| Ann. volatility | 6.1% | 3.9% | 19.2% |
| Max drawdown | −14.9% | **−8.9%** | −56.6% |
| Trades | 26 | 56 | – |
| Win rate | 50% | 39% | – |
| Profit factor | 6.30 | 2.37 | – |
| Time in market | 65% | 35% | – |
| Costs paid | $973 | – | – |

Walk-forward chose six different parameter pairs across 20 folds (50/100, 20/200, 10/100, 10/200, 20/100, 20/50);
three test years had zero trades. The full parameter grid is positive (Sharpe 0.23–0.51) but the optimum is
unstable. Aggressive sizing (2% risk, 100% cap): +158% full period vs +160% B&H, Sharpe 0.44 vs 0.31, max DD −27% vs
−57%, kill switch tripped in March 2020 and the run was flat afterwards.

### 3.2 `mean_reversion` on SP500, 2000-01-03 → 2022-12-28

| | Full period, fixed 20/2.0/0.5 | Walk-forward OOS (2003–2022) | Buy & hold (same OOS window) |
|---|---|---|---|
| Total return | +2.0% | +36.8% | +318.5% |
| CAGR | +0.09% | +1.58% | +7.43% |
| Sharpe | 0.04 | 0.45 | 0.47 |
| Max drawdown | −14.2% | −12.9% | −56.6% |
| Trades | 150 | 156 | – |
| Win rate | 59% | 63% | – |
| Profit factor | 1.03 | 1.57 | – |
| Time in market | 12% | 14% | – |
| Costs paid | $4,074 | – | – |

Break-even with fixed parameters (costs ate $4k of $6k gross). The parameter surface is **fragile** (RESEARCH.md):
the best point's neighbours have negative Sharpe and two grid points trip the kill switch. The 2008 fold lost 10.9%.

### 3.3 GOOG (real OHLCV), 2005-06-01 → 2013-03-01

| | MA full | MA OOS | MR full | MR OOS | B&H full | B&H OOS |
|---|---|---|---|---|---|---|
| Total return | +10.2% | +2.8% | +6.5% | +2.6% | +179.9% | +41.4% |
| Sharpe | 0.24 | 0.16 | 0.40 | 0.20 | 0.57 | 0.39 |
| Max drawdown | −17.3% | −7.9% | −3.5% | −5.4% | −65.3% | −56.1% |
| Trades | 11 | 15 | 44 | 45 | – | – |

Positive but tiny, far below buy-and-hold, and with 11–45 trades statistically meaningless. It confirms the
close-only runs are not hiding a fill-price problem.

### 3.4 Five-stock basket (AAPL, MSFT, JPM, XOM, KO), 2000 → 2022 (unchanged from 2026-09-21)

Both full-period runs **lost money** (−18.3% and −11.3%) because the 20% kill switch tripped in the 2000–2002 bear
market and the bot then sat in cash for two decades — the kill switch doing exactly what it is configured to do,
and a demonstration that "halt forever" needs a documented human re-entry decision. Walk-forward numbers on this
basket (+1113% MA) are survivorship-biased and reset risk state per fold; do not read them as evidence.

### 3.5 Research harness additions (2026-09-24, see RESEARCH.md)

Three research candidates (breakout, buffered crossover, volatility filter) cluster with the MA baseline at
Sharpe ≈ 0.5 on the index and rank differently on GOOG. Regime analysis: every trend variant's entire advantage
comes from being flat during the 602 bear-regime days; in the 3805 sideways days they earn ≈ 0 vs +11.7%/yr for
buy-and-hold. The 2×ATR stop, not the strategy, produces most exits (16/26 MA, 69/150 MR).

## 4. Verdict

**Neither strategy has demonstrated an edge worth capital.** `ma_crossover` is a defensible risk preference (a
third of the volatility, a sixth of the drawdown, a bit better Sharpe, much lower return). `mean_reversion` is
break-even with a fragile parameter surface and crash exposure. Every run underperformed buy-and-hold on raw
return; two of eight full-period runs lost money outright.

**Does anything deserve paper trading?** Yes, as a *systems* test: `ma_crossover` on SPY (and QQQ) with default risk
settings. It trades rarely, its behaviour is predictable, and it exercises every part of the pipeline. Judge the
infrastructure (missed sessions, duplicate orders, reconciliation mismatches, realised slippage vs 3 bps), not the
P&L. `mean_reversion` is acceptable only as a second pipeline test on SPY. Neither is a candidate for the live phase
beyond the $25-per-order execution validation in LIVE_RUNBOOK.md.

What would have to be true before either deserved real money as a bet: a walk-forward Sharpe advantage over
buy-and-hold on SPY/QQQ from Alpaca with real opens that survives 2× the assumed costs; a smooth parameter
surface; a few hundred out-of-sample trades; a written kill-switch re-entry policy; and a decision on H4 (stop width).

## 5. Next steps

1. `python -m bot data fetch --symbol SPY --symbol QQQ --start 2015-01-01 --end <today>` on a machine with paper
   credentials, then rerun sections 3.1–3.2 and `python -m bot research compare --symbol SPY ...` and update this file.
2. Phase 3 paper integration per README / LIVE_RUNBOOK step 4. Compare fills with the next-open assumption.
3. Test H4 (stop width) — it changes the trade statistics of *every* strategy.
4. Only then consider the constrained live phase, and only through LIVE_RUNBOOK.md.
