# STRATEGY_SPEC — the two baseline strategies, exactly as implemented

Every statement is tagged: **[CODE]** known from the source, **[BACKTEST]** observed in the runs listed in
RESEARCH.md / REPORT.md (S&P 500 index proxy 2000–2022 close-only; GOOG OHLCV 2005–2013; 3 bps per side costs;
default risk settings), **[HYPOTHESIS]** a testable claim not yet tested here, **[SPECULATION]** opinion.

Neither strategy is claimed to have edge. Both are baselines that exercise the infrastructure.

---

## Shared machinery (applies to both)

- **Signal semantics [CODE].** `on_bar(bar) -> Signal(target ∈ {-1, 0, +1})` on the *completed* daily bar. The engine
  fills the resulting market order at the *next* bar's open (backtest) or via a market-on-open / day market order
  (execution). Repeated identical targets are ignored.
- **Sizing [CODE].** Fixed fractional: `qty = min(risk_pct·equity / stop_distance, max_position_pct·equity / price,
  cash / price)`, whole shares unless `ALLOW_FRACTIONAL`. Defaults `RISK_PER_TRADE_PCT=1%`, `MAX_POSITION_PCT=50%`.
  In `SAFE_LIVE_TEST_MODE` also `≤ SAFE_MAX_ORDER_NOTIONAL` ($25).
- **Stops [CODE].** `stop_distance = 2.0 × ATR(14)` (Wilder). Long stop = signal-bar close − 2·ATR. Checked on each
  completed bar's close; a breach queues an exit at the next open. No intraday protection; gaps fill past the stop.
- **Effect of the stop [BACKTEST].** The stop is the dominant exit for *both* strategies: 16/26 MA-crossover exits
  and 69/150 mean-reversion exits on the S&P proxy, 5/11 and 12/44 on GOOG. After a stop-out the strategy is told
  (`on_position_closed`) and may re-enter on the next bar if its condition still holds. For MA crossover this
  produces "one trend, several small stop-outs" (see holding-period distribution). The risk layer, not the
  strategy, shapes the trade statistics. Whether a looser stop for the trend strategy is better is a
  **[HYPOTHESIS]** (H4 in research/HYPOTHESES.md) that has not been tested.
- **Exposure over time [BACKTEST].** Because sizing risks 1% against a ~2%-of-price stop, positions are ~30–50% of
  equity. Average gross exposure: MA crossover 35%, mean reversion 5% (S&P proxy full sample). Buy-and-hold is 100%.
  Comparing raw returns to buy-and-hold therefore understates the strategies by construction; compare Sharpe and
  drawdown, or the "aggressive" sizing rows in REPORT.md.
- **Long vs short [CODE].** MA crossover is long/flat only. Mean reversion is long/flat by default; `allow_short=true`
  mirrors the rule on the short side. Short behaviour has **never been backtested** and `SAFE_LIVE_TEST_MODE`
  forbids it.
- **Costs [CODE/BACKTEST].** 2 bps slippage + 1 bp half-spread per side. Cost drag was 0.0%/yr (MA) and 0.2%/yr
  (MR) of initial equity. Realised slippage vs reference is measured per fill in execution (`realized_slippage_bps`).

---

## 1. `ma_crossover`

**Economic intuition [HYPOTHESIS].** Index prices exhibit medium-term momentum: after the 50-day average rises above
the 200-day, returns over the following months are on average positive and, more reliably, the deepest drawdowns
(2000–02, 2008, 2022) occur while the fast average is below the slow one. The rule is a crude regime filter, not a
return predictor. The usual behavioural story (under-reaction, herding) is **[SPECULATION]**.

**Exact rule [CODE].** Let `F_t = SMA(close, fast)`, `S_t = SMA(close, slow)`.
- Entry: `F_t > S_t` and previously flat → target +1.
- Exit: `F_t ≤ S_t` and previously long → target 0.
- No signal until both windows are full (`warmup = slow + 1` bars). Equality is flat.
- Parameters: `fast=50`, `slow=200` (grid: fast ∈ {10, 20, 50}, slow ∈ {50, 100, 200}, fast < slow).

**Holding period [BACKTEST, S&P proxy 2000–2022, 26 trades].** 10th/50th/90th percentile 1 / 20 / 372 bars, max 814.
Average 145 bars. The bimodal shape (many 1–20-bar stop-outs, a few multi-year holds) is the stop interacting with
the trend rule, as described above. GOOG: 8 / 75 / 295 bars over 11 trades.

**Turnover [BACKTEST].** 1.0× equity per year (S&P proxy), 0.67× (GOOG). About 1–2 round trips per year.

**Trade return distribution [BACKTEST, S&P].** 10th/50th/90th percentile −2.9% / +0.2% / +22.7% per trade; win rate
50%; payoff ratio 6.3; profit factor 6.3. Classic trend profile: many small losses, few large wins. With 26 trades
the profit factor is dominated by 3–4 trades (2003–07, 2009–11, 2012–15, 2016–18) — **[BACKTEST]** the best trade
made $33.6k of the $95.7k total.

**Expected regime [BACKTEST].** Bull regimes (trailing 6-month return > 10%): annualised +15.0% at Sharpe 2.4 vs
buy-and-hold +27.8% at 2.35. Bear regimes: −2.7% vs −75.7% annualised. Sideways: +0.2% (Sharpe 0.04) vs +11.7%.
That is the whole story: it *avoids* bears and *pays for it* in sideways markets.

**Failure regimes [BACKTEST/HYPOTHESIS].** (a) Sideways/choppy markets with repeated crosses (2011, 2015–16): small
losses accumulate. (b) V-shaped recoveries: the 200-day filter re-enters months after the low (2009: +8.5% vs +20.2%
B&H; 2020: +8.7% vs +16.1%). (c) Sharp corrections while long: the ATR stop exits, the trend rule stays "on", the
strategy re-enters and is stopped again (2020-Q1 daily halts ×2).

**Sensitivity to spread/slippage [BACKTEST].** Low: 1.0× turnover per year means 6 bps round trip costs ≈ 6 bps/yr.
Doubling assumed costs would not change the conclusion.

**Susceptibility to gaps [CODE/HYPOTHESIS].** Exits on the next open after a close breaches the stop, so an
overnight gap through the stop is fully borne. On close-only research data the gap risk is not visible at all; on
GOOG (real opens) the stop-out losses were larger than on the index (10th percentile −5.4% vs −2.9%).

**Susceptibility to reversals/whipsaw [BACKTEST].** High for whipsaw near a flat slow average. The buffered variant
(H2) with a 0.5% band shows the same Sharpe with 3 fewer trades — the band does not remove the problem.

**Parameter surface [BACKTEST, S&P 2000–2022].** All 8 grid points have positive Sharpe (0.23–0.51); best 50/200,
neighbours average 84% of the best. Stable, in the sense that no parameter choice in the grid loses money. But
walk-forward selection picked 6 different parameter pairs across 20 folds, so "stable surface" and "unstable
optimum" are both true.

**Assumptions required for edge [HYPOTHESIS].** Persistence of index momentum at the 6–12 month horizon; that
avoiding bear markets is worth more than the sideways losses; costs ≲ 10 bps per side. If the next decade has more
sharp, short corrections and fewer extended bears, the strategy will underperform cash-plus-buy-and-hold.

---

## 2. `mean_reversion`

**Economic intuition [HYPOTHESIS].** Short-horizon (days) index returns are weakly negatively autocorrelated:
after a sharp multi-sigma drop relative to the recent mean, the next few days are on average positive
(liquidity provision / over-reaction). The effect is small, decays quickly, and reverses in crashes.

**Exact rule [CODE].** Over the last `lookback` closes let `μ` and `σ` (sample std, ddof=1); `z_t = (close_t − μ)/σ`.
- Entry: flat and `z_t < −entry_z` → target +1. (`allow_short`: flat and `z_t > +entry_z` → −1.)
- Exit: long and `z_t > −exit_z` → 0. (short and `z_t < +exit_z` → 0.)
- `σ ≤ 0` → no signal. `warmup = lookback + 1`.
- Parameters: `lookback=20`, `entry_z=2.0`, `exit_z=0.5`, `allow_short=false` (grid: lookback ∈ {10, 20, 40},
  entry_z ∈ {1.5, 2.0, 2.5}, exit_z ∈ {0.0, 0.5}).

**Holding period [BACKTEST, S&P, 150 trades].** 10/50/90th percentile 1 / 4 / 8 bars, max 13. Average 4.6 bars.
GOOG: 2 / 6.5 / 12 bars.

**Turnover [BACKTEST].** 5.9× equity per year (S&P), 2.2× (GOOG). About 6–7 round trips per year.

**Trade return distribution [BACKTEST, S&P].** 10/50/90th percentile −4.0% / +0.5% / +3.3%; win rate 59%; payoff
0.72; profit factor 1.03 with fixed parameters. The typical loss is larger than the typical win; the strategy needs
its ~60% hit rate to break even, and costs (0.2%/yr drag on a 5%-exposed position ≈ 4%/yr of the *position*) eat the
rest.

**Expected regime [BACKTEST].** Positive but small in low-vol, sideways and bull regimes (Sharpe 0.0–1.2); negative
in bear regimes (−9.0% annualised, Sharpe −1.5). The 2008 walk-forward fold lost 10.9%.

**Failure regimes [BACKTEST/HYPOTHESIS].** Crashes: a "2-sigma dip" in a crash is followed by another; the stop
saves the position but the strategy re-enters (69 stop exits in 150 trades). Trending declines: repeated small
losses. It is a short-volatility-like payoff without the premium.

**Sensitivity to spread/slippage [BACKTEST/HYPOTHESIS].** High: 5.9× turnover means 6 bps round trip ≈ 35 bps/yr
of equity, ≈ 4%/yr of the position size. At 10 bps per side the fixed-parameter version loses money. This is the
strategy most exposed to the difference between backtest and realised slippage.

**Susceptibility to gaps.** High: entries are triggered by large down moves, which cluster with overnight gaps;
exits at the next open after a stop breach compound it.

**Parameter surface [BACKTEST, S&P 2000–2022].** 18 grid points: Sharpe from −0.34 to +0.37; 78% positive; two
points tripped the kill switch; the best point (20/1.5/0.5) has neighbours averaging *negative* Sharpe. Flagged
**FRAGILE** by the surface tool. Walk-forward selected 9 different parameter sets over 20 folds. Any positive
walk-forward number for this strategy should be read as selection noise until proven otherwise.

**Assumptions required for edge [HYPOTHESIS].** Short-horizon reversal persists; execution at or near the next
open at ≤ 5 bps per side; no crash during the position (the stop does not fully protect against gaps). None of
these can be verified from the backtests here.

---

## Verdict on the two baselines (repeated from REPORT.md)

- `ma_crossover`: defensible as a *risk-preference* (lower drawdown, lower return, slightly higher Sharpe than
  buy-and-hold). Not alpha. ~26–56 trades over 20 years cannot establish anything.
- `mean_reversion`: break-even with fixed parameters, fragile surface, cost-sensitive, crash-exposed. Not a
  candidate for capital; acceptable only as a second infrastructure test on SPY.
