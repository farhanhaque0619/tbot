# RESEARCH — harness results (Phases 7 & 8)

Produced with `python -m bot research compare|surface` on 2026-09-24. Data: S&P 500 index proxy (close-only,
2000-01-03 → 2022-12-28) and GOOG (real OHLCV, 2005-06-01 → 2013-03-01). Same engine, costs (3 bps/side) and risk
limits (1% risk, 2×ATR stop, 50% cap, 20% kill switch) as execution. `cash` and `buy_and_hold` are computed by the
same engine. Full tables: `reports/research_compare_*.md`, `reports/research_surface_*.md`.

**No strategy here is selected for capital.** The purpose of the harness is to make comparisons honest and cheap.

## Sample cuts — S&P 500 proxy

Cuts are contiguous and chronological: in-sample = first 60%, validation = next 20%, held-out test = last 20%.
Candidate strategies (H1–H3) were *designed* with knowledge of the full period, so their held-out numbers are not
truly out of sample; only the walk-forward row has that property.

| Cut | Strategy | Total | CAGR | Vol | Sharpe | MaxDD | Hit | PF | Turnover/y | Hold (bars) | Trades | Gross exp |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| full 2000–22 | cash | 0.0% | 0.0% | 0.0% | 0.00 | 0.0% | – | – | 0 | – | 0 | 0% |
| | buy_and_hold | +163.7% | +4.3% | 19.1% | 0.32 | −54.8% | – | – | 0.12 | 5784 | 1 | 100% |
| | ma_crossover | +95.7% | +3.0% | 6.1% | 0.51 | −14.9% | 50% | 6.30 | 1.02 | 145 | 26 | 35% |
| | mean_reversion | +2.0% | +0.1% | 3.6% | 0.04 | −14.1% | 59% | 1.03 | 5.93 | 4.6 | 150 | 5% |
| | donchian_breakout (H1) | +13.6% | +0.6% | 3.7% | 0.17 | −10.3% | 43% | 1.28 | 3.48 | 29 | 81 | 20% |
| | ma_crossover_buffered (H2) | +98.3% | +3.0% | 6.3% | 0.51 | −15.2% | 48% | 7.99 | 0.91 | 163 | 23 | 35% |
| | trend_vol_filter (H3) | +91.1% | +2.9% | 5.8% | 0.51 | −10.2% | 40% | 3.26 | 1.92 | 79 | 48 | 35% |
| in-sample 2000–13 | buy_and_hold | +23.7% | +1.6% | 19.9% | 0.18 | −54.8% | | | | | | |
| | ma_crossover | +47.7% | +2.9% | 5.4% | 0.55 | −10.9% | 58% | 28.2 | 0.89 | 356 | 12 | 30% |
| | mean_reversion | −1.8% | −0.1% | 3.5% | −0.02 | −14.1% | 58% | 0.96 | 5.82 | 4.9 | 91 | 5% |
| | H1 / H2 / H3 Sharpe | | | | 0.19 / 0.52 / 0.44 | | | | | | | |
| validation 2013–18 | buy_and_hold | +54.5% | +9.9% | 12.4% | 0.83 | −14.0% | | | | | | |
| | ma_crossover | +11.8% | +2.5% | 4.1% | 0.61 | −6.0% | 33% | 10.1 | 1.33 | 282 | 6 | 25% |
| | mean_reversion | +6.7% | +1.4% | 3.1% | 0.46 | −4.0% | 61% | 1.58 | 6.69 | 3.8 | 31 | 5% |
| | H1 / H2 / H3 Sharpe | | | | −0.08 / 0.78 / 0.56 | | | | | | | |
| held-out 2018–22 | buy_and_hold | +40.1% | +7.6% | 22.0% | 0.44 | −33.6% | | | | | | |
| | ma_crossover | +12.4% | +2.6% | 7.2% | 0.39 | −14.9% | 50% | 4.47 | 1.15 | 84 | 8 | 29% |
| | mean_reversion | −2.6% | −0.6% | 4.3% | −0.11 | −11.6% | 57% | 0.84 | 5.51 | 4.8 | 28 | 5% |
| | H1 / H2 / H3 Sharpe | | | | 0.25 / 0.45 / 0.55 | | | | | | | |

Other full-sample metrics (S&P): time underwater 79% (MA) / 98% (MR) / 100% (B&H); longest underwater 719 / 4442 /
∞ bars; 5%-tail day −0.6% / −0.1% / −1.2%; worst day −4.8% / −3.2% / −11.7%; cost drag 0.0% / 0.2% / 0.0% per year.

## Walk-forward (3y train / 1y test, 20 folds, params chosen on train Sharpe only) — S&P proxy

| Strategy | OOS total | OOS CAGR | OOS Sharpe | OOS MaxDD | Trades | B&H total | B&H Sharpe |
|---|---|---|---|---|---|---|---|
| ma_crossover | +53.0% | +2.2% | 0.55 | −8.9% | 56 | +318.5% | 0.47 |
| mean_reversion | +36.8% | +1.6% | 0.45 | −12.9% | 156 | | |
| donchian_breakout (H1) | +30.4% | +1.3% | 0.38 | −8.9% | 85 | | |
| ma_crossover_buffered (H2) | +64.1% | +2.5% | 0.72 | −5.1% | 30 | | |
| trend_vol_filter (H3) | +41.5% | +1.8% | 0.49 | −7.3% | 63 | | |

## Regimes — S&P proxy, full-sample daily returns, regimes labelled from the benchmark's trailing data

| Regime (days) | buy_and_hold | ma_crossover | mean_reversion | H1 | H2 | H3 |
|---|---|---|---|---|---|---|
| bull (1252) ann. ret / Sharpe | +27.8% / 2.35 | +15.0% / 2.41 | +2.0% / 1.15 | +6.2% / 1.20 | +15.2% / 2.36 | +15.1% / 2.43 |
| bear (602) | −75.7% / −2.03 | −2.7% / −0.59 | −9.0% / −1.52 | −0.5% / −0.41 | −3.3% / −0.74 | −1.8% / −0.58 |
| sideways (3805) | +11.7% / 0.72 | +0.2% / 0.04 | +0.8% / 0.22 | −0.8% / −0.22 | +0.3% / 0.05 | +0.2% / 0.03 |
| high-vol (1441) | +7.3% / 0.23 | +0.4% / 0.06 | +0.5% / 0.09 | −0.4% / −0.15 | +0.6% / 0.08 | +0.6% / 0.10 |
| low-vol (4323) | +5.6% / 0.45 | +4.0% / 0.69 | 0.0% / 0.00 | +1.0% / 0.25 | +4.1% / 0.69 | +3.8% / 0.65 |

Reading: every trend variant earns its keep only by being flat in bears (602 days). In the 3805 sideways days
they are roughly zero while buy-and-hold makes +11.7%/yr. Mean reversion is negative exactly where it is supposed to
shine (high-vol, bear): it is not a crash hedge, it is crash-exposed.

## GOOG (real OHLCV, 2005–2013) — full sample

| Strategy | Total | CAGR | Sharpe | MaxDD | Trades | Turnover/y | WF OOS Sharpe |
|---|---|---|---|---|---|---|---|
| buy_and_hold | +178.9% | +14.2% | 0.56 | −65.3% | 1 | 0.28 | 0.39 |
| ma_crossover | +10.2% | +1.3% | 0.24 | −17.3% | 11 | 0.67 | 0.16 |
| mean_reversion | +6.5% | +0.8% | 0.40 | −3.5% | 44 | 2.19 | 0.20 |
| donchian_breakout | +14.8% | +1.8% | 0.43 | −7.6% | 18 | 1.15 | 0.06 |
| ma_crossover_buffered | +4.0% | +0.5% | 0.12 | −17.9% | 12 | 0.72 | 0.41 |
| trend_vol_filter | +17.3% | +2.1% | 0.52 | −10.7% | 45 | 2.76 | **−0.43** |

The only dataset with real opens. Rankings flip relative to the index (H3 is best in-sample and worst out of
sample). With 11–45 trades none of it is meaningful, which is the point: a single-stock, 8-year test cannot rank
these strategies.

## Parameter surfaces — S&P proxy 2000–2022 (full grids in `reports/research_surface_*.md`)

| Strategy | Grid points | Sharpe range | Positive share | Best | Neighbour/best | Verdict |
|---|---|---|---|---|---|---|
| ma_crossover | 8 | 0.23 – 0.51 | 100% | 50/200 | 0.84 | stable (no grid point loses money) |
| mean_reversion | 18 | −0.34 – 0.37 | 78% | 20 / 1.5 / 0.5 | −0.28 | **FRAGILE**: neighbours of the best are negative; 2 points hit the kill switch |
| donchian_breakout | 7 | 0.05 – 0.39 | 100% | 100/50 | 0.66 | stable but weak |
| ma_crossover_buffered | 8 | 0.14 – 0.53 | 100% | 50/200/0.5% | 0.96 | flat: the band changes little |
| trend_vol_filter | 5 | 0.27 – 0.54 | 100% | cap 0.35 | 0.94 | monotone in the cap; cap=∞ equals plain MA |

## What this does and does not show

- Trend-following variants on the index cluster at Sharpe ≈ 0.5 with max drawdown 10–15% vs buy-and-hold 0.32 /
  −55%. That is a robust *shape* (all parameters, all cuts) but a modest *magnitude* (+2–3%/yr on ~35% exposure).
- H2 (buffered MA) has the best walk-forward Sharpe (0.72) with 30 trades. Thirty trades. Do not act on it.
- H3 (volatility filter) reduces drawdown on the index and blows up on GOOG out of sample. Instrument-specific.
- Mean reversion's parameter surface is fragile. Its positive walk-forward result (+36.8%, Sharpe 0.45) coexists
  with a break-even fixed-parameter result and a negative-neighbour surface — that is what selection noise looks like.
- All of this is on a close-only index proxy and one stock. Re-run everything on SPY/QQQ from Alpaca before any
  belief is updated: `python -m bot research compare --symbol SPY --start 2015-01-01 --end <today>`.

## Process rule (Phase 8)

No parameter or strategy change reaches execution unless: (1) it is written up as a hypothesis in
`research/HYPOTHESES.md` with rationale, exact rule, expected failure mode and cost estimate; (2) its parameter
surface is reported in full; (3) it survives walk-forward on the actual traded instruments; (4) the diff is
reviewed and tests pass. The bot never does any of this by itself.
