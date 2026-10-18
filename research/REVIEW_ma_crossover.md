# ma_crossover on SPY and QQQ — strategy review (2026-10-18)

What this document is: a diagnosis of the V1 baseline from the operator's real Alpaca runs (V1_5_AUDIT.md §2.1, §2.2 and
the 2026-10-18 run) plus mechanism diagnostics on the S&P proxy, and a ranked list of hypotheses to test under the
research protocol. What it is not: a tuned version of the strategy. Every "improvement" below is a hypothesis until it
passes cuts B and C in `python -m bot research report`; nothing here changes the policy or the live gates.

## 1. What the strategy actually is

Rule: long SPY when the 50-day SMA of close is above the 200-day SMA, else flat. Decided at the close, filled at the
next open. Risk layer: size = 1% of equity divided by a 2×ATR(14) stop distance, capped at 50% of equity; exit on a
close through the stop; re-enter the next bar if the signal still holds. No shorts, no leverage.

Two consequences dominate everything below. First, with SPY's ATR near 1% of price the stop distance is about 2%, so
1% risk sizes the position at about 50% of equity: the cap binds and **the strategy is always a half-size position**.
Annualised volatility 4.9–5.5% against 17.5–17.9% for buy-and-hold is that half size times 60–77% time in market. Any
comparison of total return with buy-and-hold is a comparison of a 0.3-beta position with a 1.0-beta one. Second, the
stop is tight: on the proxy 16 of 26 exits were stops, not signal changes.

## 2. The numbers (real Alpaca data)

| | SPY 2017→2026-09 (frozen) | SPY 2016→2026-09 (10-18 run) | QQQ 2017→2026-09 | SPY WF OOS 2020→2026 | QQQ WF OOS 2020→2026 |
|---|---|---|---|---|---|
| Total return | +29.4% | +44.3% | +48.2% | +12.7% | +26.1% |
| CAGR | +2.7% | +3.5% | +4.1% | +1.8% | +3.5% |
| Sharpe | 0.57 | 0.65 | 0.73 | 0.65 | 0.91 |
| Max drawdown | −11.3% | −11.3% | −10.2% | −5.6% | −5.8% |
| Ann. vol | 4.9% | 5.5% | – | 2.8% | – |
| Trades | 10 | 11 | 6 | 15 | 16 |
| Time in market | 63% | 77% | 64% | 42% | 54% |
| Buy & hold total / Sharpe / MaxDD | +243% / 0.80 / −34% | +284% / 0.80 / −34% | +523% / 0.94 | +140% / 0.76 | +247% / 0.87 |

Research cuts on SPY (§2.2): in-sample 2017→2022-10 Sharpe 0.25 vs buy-and-hold 0.58; validation 2022-10→2024-10
1.40 vs 1.49; held-out 2024-10→2026-09 1.19 vs 0.96. Regimes (benchmark-defined): bull +10.0%/yr Sharpe 2.30; bear
−9.1%/yr Sharpe −1.52; sideways −0.5%/yr; 1,358 of 2,320 days were "sideways".

## 3. Reading the numbers honestly

- **It is a drawdown-reduction trade, not a return strategy.** Max drawdown a third of buy-and-hold, Sharpe below
  buy-and-hold on both ETFs in-sample and roughly equal out-of-sample. With half sizing the strategy forgoes most of the
  index's drift and buys back lower volatility. Whether that trade is worth making is a portfolio question, not a
  strategy one.
- **The sample is too small for any claim.** 6–16 trades in ten years. The 95% interval on a Sharpe estimated from
  ~1,900 OOS daily returns is about ±0.7; "0.65 vs 0.76" is noise. The protocol's deflated Sharpe will say the same.
- **The walk-forward is unstable in the way that matters.** Chosen parameters per fold: 20/50, 20/50, 20/50 (or
  10/100), 10/100, 20/100, 10/200, 10/200, 10/200. Every fold picks a different speed; the 2022 fold chose 10/100 and
  lost −4.6% (Sharpe −1.93) while 2024 chose 10/200 and made zero trades. A selection procedure that cannot settle on a
  parameter is telling you the surface is flat and the choice is luck (§2.2's surface runs said the same: Sharpe
  0.14–0.53 across 50/200 neighbours, "stable neighbourhood" but no peak).
- **Bear markets are where it loses, which is backwards for a trend filter.** −9.1%/yr in bear regimes: the crossover
  lags, so it is still long into the first leg down, gets stopped, re-enters on a bounce, gets stopped again. 2022 is the
  textbook case (five trades, −4.6%). The thing it is supposed to do, step aside in bear markets, is the thing it does
  worst; what it does well is stay long in bulls at half size.
- **Costs are not the problem.** 187–243 dollars over ten years on 100k; 6 bps round trip. Do not spend effort here.

## 4. Mechanism diagnostics (S&P proxy 2000–2022, 26 trades; directional evidence only)

| Stop multiple | Return | Sharpe | MaxDD | Trades | Median hold | Stop exits | P&L from stop exits | P&L from signal exits |
|---|---|---|---|---|---|---|---|---|
| 2.0 (production) | +95.7% | 0.51 | −14.9% | 26 | 29 days | 16 of 26 | −14,992 | +110,714 |
| 3.0 | +69.9% | 0.45 | −14.9% | 20 | 110 days | 10 of 20 | −15,032 | +84,947 |
| 4.0 | +52.8% | 0.44 | −12.0% | 19 | 113 days | 9 of 19 | −9,336 | +62,183 |
| none (100×) | +2.4% | 0.44 | −0.7% | 10 | 322 days | 0 | 0 | +2,434 |

Three things fall out. The stop does most of the exiting and all of its exits lose money in aggregate; the signal
exits carry the whole return. Widening the stop does not help because the *size* shrinks with the stop distance
(1% risk / wider stop = smaller position): the "no stop" row is not a stopless strategy, it is a 1%-of-equity position.
So **the stop is really the position-sizing rule wearing a risk-control costume**, and the two cannot be evaluated
separately in the current engine. Finally the median holding period of 29 days under the production stop, against a
50/200 signal that changes a few times a year, confirms the churn: stop out, re-enter, stop out.

## 5. What "improve" can legitimately mean

Not: re-fitting 50/200, adding a filter chosen by looking at 2022, or widening the stop because the proxy table above
looks better at 2.0. All three are in-sample selection on the same data that produced the complaint.

Yes: (a) change the *mechanism* for an economic reason, (b) declare the parameter surface before running, (c) run it
through `python -m bot research report` on cuts B (2019–2021) and C (2022–2023) with D sealed, (d) accept the verdict.
The protocol already exists and the daily SPY/QQQ data on the Mac is enough for M1.

### Hypotheses, ranked by how directly they attack the diagnosis

1. **Separate sizing from the stop (M1 is this).** Size by target volatility (10% annualised / realised vol, capped at
   60%) and make the exit a slow signal, not a tight stop. This removes the churn in §4 and lets the position be larger
   when the market is quiet and smaller when it is not, instead of always half size. M1 as written: 12-month-minus-
   1-month momentum as the on/off switch, vol-managed weight, weekly rebalance, a 4-sigma-week catastrophe stop only.
   **Runnable today**: `python -m bot research report --module M1`. Expected: lower turnover than 50/200 with the stop,
   similar drawdown, a Sharpe that depends on whether 12-1 momentum has worked on US indices since 2019 (it has been
   weak; the honest prior is "about the same as 50/200").
2. **Replace the binary filter with graded exposure.** Instead of 0 or 50%, hold exposure proportional to a trend score
   (distance of price from the 200-day in ATR units, clipped to 0…1) times a vol-managed weight. Economic reason: the
   lag that costs −9%/yr in bears is the lag of a binary switch; a graded rule starts cutting exposure as the trend weakens
   rather than 40 days later. Surface: trend lookback {126, 200, 252}, clip width {1, 2, 3} ATR. Needs a new module
   (small: a daily-close module like M1 with `target_weight`).
3. **Make the stop a trailing exit at a trend scale, or remove it.** If sizing is handled by (1), the stop can be a
   genuine catastrophe guard (e.g. 4–6 ATR or a close below the 200-day), not the primary exit. Surface: {3, 4, 6} ATR
   trailing, or "200-day close only". Test as a variant of (1); do not test it alone, because alone it just shrinks the
   position.
4. **Diversify the signal, not the symbol.** Running the same rule on QQQ gave a better-looking Sharpe (0.91 OOS) on
   6–16 trades, which is sampling noise with a more volatile asset; it is not diversification. Combining a slow trend
   rule with an uncorrelated mechanism (M2's intraday momentum has zero overnight exposure by construction) is the
   portfolio-level improvement the V1.5 allocator was built for. Evidence for M2 does not exist yet; it needs the
   minute data (`data fetch --timeframe 1m`, 2019–2023, SPY and QQQ) and then `research report --module M2`.
5. **Do nothing to the rule and change what it is for.** If the goal is "lower drawdown than the index with some of its
   return", the current rule already does that at ~0.3 beta; the question becomes how much of the account it should
   manage next to a buy-and-hold core, which is an allocation decision the policy expresses (`max_module_gross_pct`),
   not a strategy change.

Things I would **not** test: shorter crossovers (the walk-forward already tried 10/50 to 50/200 and found nothing
stable), adding mean_reversion (negative out of sample on both ETFs), volatility-targeting the existing stop-based
sizing (it fights the ATR rule), and any filter motivated by 2022 alone.

## 6. How to run the first two tests this week

```
python -m bot research run --module M1 --cut B
python -m bot research run --module M1 --cut C
python -m bot research report --module M1
```

The report writes `research/RESULTS_V1_5.md` with the surface on B, validation with a bootstrap interval on C, six cost
stresses, causal regimes, attribution and the five pass/fail lines. A FAIL is a result; it is what honest protocols
mostly produce. Hypothesis 2 needs a module written first (one file, same shape as `bot/strategies/v15/m1_vol_trend.py`)
and a declared surface added to `bot/research/protocol.py`; say the word and I will write it as a research candidate,
not as something the daemon can trade.

## 7. What could be wrong in this review

The proxy diagnostics are on an index series with synthetic opens, not SPY, and on 2000–2022, not the period the
account will trade. The regime labels are benchmark-defined, so "bear" days are known only after the fact; the causal
version in the protocol will give different counts. The half-size argument assumes ATR ≈ 1% of price, which held for SPY
over this decade but not in March 2020 (when the stop widened and the cap did not bind, i.e. the strategy was smaller
exactly when the market was most volatile, which is the one thing the ATR rule gets right). And ten trades cannot
distinguish a good rule from a lucky one in either direction; everything above is diagnosis of mechanism, not of edge.
