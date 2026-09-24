# HYPOTHESES — research register (Phase 8)

Every candidate must have all eight fields before it is tested, and results are recorded as parameter surfaces
(`python -m bot research surface`) and walk-forward runs, never as a single best number. A hypothesis is
**closed** when its out-of-sample test on the actual traded instruments (SPY/QQQ from Alpaca) is recorded here.
Nothing in this file changes execution; promotion requires the process in RESEARCH.md.

| Field | Meaning |
|---|---|
| Hypothesis | one falsifiable sentence |
| Rationale | economic / behavioural reason it could be true |
| Rule | exact, implementable, no discretion |
| Failure mode | the regime in which it should lose |
| Cost estimate | turnover × round-trip cost |
| Sensitivity | parameter surface summary |
| OOS test | walk-forward result on the target instrument |
| Benchmark | vs cash / buy-and-hold / the baselines |

---

## H1 — Breakout confirmation (`donchian_breakout`) — TESTED on proxy, OPEN on SPY
- **Hypothesis.** Entering on a close above the prior 55-day high (exit below the prior 20-day low) captures trend
  continuation earlier than a 50/200 crossover with fewer whipsaws.
- **Rationale.** Range expansion after consolidation; the rule is price-based and lag-free.
- **Rule.** `bot/research/candidates.py::DonchianBreakout`. Long/flat.
- **Failure mode.** False breakouts in ranges; entries at the worst point of a spike.
- **Cost estimate.** 3.5× turnover/yr × 6 bps ≈ 21 bps/yr of equity.
- **Sensitivity (S&P proxy 2000–22).** 7 points, Sharpe 0.05–0.39, all positive; best 100/50; neighbours 66% of best.
- **OOS (proxy walk-forward).** +30.4%, Sharpe 0.38, MaxDD −8.9%, 85 trades. Worse than plain MA crossover (0.55).
- **Benchmark.** Below MA crossover on every cut; negative in validation. **Verdict: not promising on the index.**

## H2 — Whipsaw reduction by hysteresis (`ma_crossover_buffered`) — TESTED on proxy, OPEN on SPY
- **Hypothesis.** Requiring the fast MA to exceed the slow by a band before entering (and fall below by the band
  before exiting) removes the small repeated losses around a flat slow average.
- **Rationale.** Most MA-crossover losers are 1–20-bar trades around a cross; a band filters marginal crosses.
- **Rule.** `MACrossoverBuffered`, band ∈ {0, 0.5%, 1%, 2%}.
- **Failure mode.** Later entry/exit in fast reversals gives back more; the band is a free parameter to overfit.
- **Cost estimate.** 0.9× turnover/yr × 6 bps ≈ 5 bps/yr.
- **Sensitivity.** 8 points, Sharpe 0.14–0.53; for 50/200 the band changes Sharpe by < 0.03. Flat surface: the
  band does *not* fix the whipsaw problem (trades 26 → 23).
- **OOS (proxy walk-forward).** +64.1%, Sharpe 0.72, MaxDD −5.1%, **30 trades**. Best of the set, and statistically
  indistinguishable from the baseline given the trade count.
- **Benchmark.** ≈ MA crossover on all cuts. **Verdict: no evidence the band adds anything; keep as a control.**

## H3 — Volatility-regime filter (`trend_vol_filter`) — TESTED on proxy, OPEN on SPY
- **Hypothesis.** Being long only when trailing 21-day realised volatility is below a cap avoids the crash phases
  where trend strategies take their largest losses.
- **Rationale.** Index drawdowns and volatility spikes coincide; the filter is a cheap crisis detector.
- **Rule.** `TrendVolFilter`: MA 50/200 long AND `rvol_21 < cap`.
- **Failure mode.** Misses V-shaped recoveries that begin in high volatility (2009, 2020). Instrument-specific:
  single stocks have structurally higher volatility, so a cap tuned on the index is wrong for them.
- **Cost estimate.** 1.9× turnover/yr × 6 bps ≈ 12 bps/yr.
- **Sensitivity.** Monotone in the cap up to 0.35 (Sharpe 0.27 → 0.54); cap=∞ reproduces plain MA (0.51). Max
  drawdown −10.2% vs −14.9% at cap 0.25.
- **OOS (proxy walk-forward).** +41.5%, Sharpe 0.49, MaxDD −7.3%. **On GOOG walk-forward: Sharpe −0.43** (best
  in-sample, worst out of sample).
- **Benchmark.** Slightly lower return, lower drawdown than MA on the index; fails on the single stock.
  **Verdict: drawdown reduction is real on the proxy; instability across instruments is a red flag.**

## H4 — Stop width for the trend strategy — OPEN, NOT TESTED
- **Hypothesis.** A 2×ATR stop is far too tight for a 200-day trend rule; most MA-crossover exits are stop-outs
  (16 of 26 on the proxy) that are immediately re-entered. A wider stop (4–6×ATR) or a trailing stop would reduce
  churn without changing the drawdown profile much.
- **Rationale.** Stop distance should match the holding horizon; 2×ATR ≈ 2–3% is a 1–2 week move on the index.
- **Rule.** `ATR_STOP_MULT ∈ {2, 3, 4, 6}` as a *risk* parameter, with `RISK_PER_TRADE_PCT` held fixed (position
  size shrinks as the stop widens — this must be reported, not hidden).
- **Failure mode.** Larger loss per trade when a real bear starts; fewer trades → even less statistical power.
- **Cost estimate.** Lower turnover than baseline.
- **Sensitivity / OOS / Benchmark.** Not run. Requires a harness extension to sweep risk parameters (currently
  the surface tool sweeps strategy parameters only). This is the highest-value next experiment because it is
  about the *risk layer*, which is shared by everything.

## H5 — Short-horizon reversal is cost-sensitive (`mean_reversion`) — TESTED on proxy
- **Hypothesis.** The mean-reversion baseline's expectancy is below realistic transaction costs.
- **Sensitivity.** Fragile surface (RESEARCH.md); break-even at 3 bps/side with fixed parameters.
- **Next.** Rerun the surface with `SLIPPAGE_BPS ∈ {2, 5, 10}` on SPY. If Sharpe turns negative at 5 bps the
  strategy is retired as a baseline for anything but infrastructure tests.

## Not started (ideas from the brief, in priority order)
- Relative strength / cross-sectional momentum across SPY, QQQ, IWM, EFA, TLT (needs multi-asset data first).
- Volume confirmation of breakouts (H1 + volume z-score > 1).
- Overnight vs intraday return decomposition (needs real opens: SPY/QQQ from Alpaca, not the close-only proxy).
- Market breadth (needs constituent data; out of scope for this repo's data layer).
