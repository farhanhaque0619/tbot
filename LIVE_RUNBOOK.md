# LIVE_RUNBOOK — first controlled live validation (Phase 17)

Purpose: validate **execution plumbing** with real money in the smallest possible way. The symbol is chosen for
liquidity and fractionability (SPY), not for expected return. Nothing here is a bet.

Preconditions that are true today (2026-09-24): the paper phase has **not** been run yet (no network to Alpaca in
the build environment). Do not start this runbook until Phase 3 (paper integration) has passed on your machine.

Time budget: about one hour across two market sessions. Keep a terminal log.

## 0. Credentials
- Paper keys in `.env` as `ALPACA_PAPER_API_KEY/SECRET_KEY`; live keys as `ALPACA_LIVE_API_KEY/SECRET_KEY`.
  Never the same pair in both slots. Never in shell history (`set -o history` off, or use an editor).
- If any key was ever pasted into chat, a ticket, a screenshot: rotate it in the Alpaca dashboard first.
- `git status` must show `.env` untracked and ignored: `git check-ignore .env` prints `.env`.

## 1. Confirm git clean
```
git status --short            # empty
git log --oneline -3
```
## 2. Run all tests
```
python -m pytest -q           # all pass; 7 integration tests skipped is expected
```
## 3. Secret scan
```
git grep -nE "AK[A-Z0-9]{20,}|PK[A-Z0-9]{16,}" -- . ':!tests/test_data_and_config.py' && echo LEAK || echo clean
git log -p --all | grep -cE "AK[A-Z0-9]{20,}|PK[A-Z0-9]{16,}"    # must print 0
```
## 4. Paper integration (must pass before anything live)
```
python -m bot doctor --paper
RUN_ALPACA_INTEGRATION=1 python -m pytest tests/integration -q -s                 # read-only
RUN_ALPACA_INTEGRATION=1 RUN_ALPACA_INTEGRATION_ORDERS=1 python -m pytest tests/integration -q -s   # submits ~$1.50 paper orders
python -m bot paper --strategy ma_crossover --symbol SPY --once                    # around 19:30 ET, twice; second run must submit nothing
python -m bot dashboard
```
## 5–8. Query the live account READ-ONLY and confirm manually
```
TRADING_ENV=live python -m bot status --live
```
Compare against the Alpaca web dashboard: **equity** (≈ $100), **open positions** (expected: none), **open
orders** (expected: none), account status ACTIVE, `trading_blocked=False`, `account_blocked=False`,
`shorting_enabled` irrelevant (safe mode forbids shorts), `multiplier` (margin) irrelevant (safe mode uses cash
only). Write the numbers down.

## 9. Confirm the environment banner says LIVE
`python -m bot doctor --live` must print the `!!!! TRADING ENVIRONMENT: LIVE — REAL MONEY !!!!` banner and end with
`doctor: OK`. If it prints `MISMATCH` for the account-number check, stop: the live slot holds a paper key.

## 10. Confirm SAFE_LIVE_TEST_MODE
`doctor --live` prints the safe-mode table. Defaults for the $100 account:

| Limit | Default | Meaning |
|---|---|---|
| SAFE_MAX_ORDER_NOTIONAL | $25 | no single order larger than this |
| SAFE_MAX_GROSS_EXPOSURE | $50 | sum of |position notional| |
| SAFE_MAX_DAILY_LOSS | $5 | below the day's start → no new entries |
| SAFE_MAX_ACCOUNT_DRAWDOWN | $10 | below peak equity → kill switch |
| SAFE_MAX_POSITIONS | 1 | one strategy position at a time |
| SAFE_ALLOWED_SYMBOLS | SPY,QQQ | allow-list |
| shorting / margin / extended hours / options / crypto / pyramiding / Kelly | forbidden | not configurable in safe mode |

The bot cannot raise these. Only you can, by editing `.env`, and doing so invalidates any existing arm.

## 11. Confirm all risk limits
`python -m bot live check` prints the 11 interlock gates. Expected before arming: everything PASS except
`operator_confirmation_and_armed`. `LIVE_AUTONOMOUS_TRADING` must print `False`.

## 12. Enable live order capability explicitly (arm)
In a second terminal, during the *regular session* (fractional orders need `day` TIF and an open market):
```
TRADING_ENV=live python -m bot live arm
```
Read the safe-mode table, type `ACK`, then type exactly `I UNDERSTAND THIS USES REAL MONEY`. The arm expires after
`LIVE_ARM_TTL_MINUTES` (30) and on any restart of a trading process.

## 13. ONE controlled small order
```
TRADING_ENV=live python -m bot trade --live --strategy ma_crossover --symbol SPY --once
```
What happens: one cycle. If the strategy's current desired exposure is flat, **no order is sent** and the decision
record says why — that is a successful test of the gates, not a failure. To force an execution test regardless of
the signal, use the paper-validated integration order path instead of changing the strategy:
```
TRADING_ENV=live RUN_ALPACA_INTEGRATION=1 RUN_ALPACA_INTEGRATION_ORDERS=1 \
  python -m pytest tests/integration/test_alpaca_paper.py::test_submit_read_duplicate_cancel -q -s
```
(This test refuses to run unless the broker is paper. For a live smoke order, use the `trade --live --once` path
only; do not modify the test's guard.)

## 14–15. Verify broker acknowledgement and fill
`logs/decisions_live.jsonl` last record: `order_decision: submit`, an `order_id`, a `broker_request_id`.
Alpaca dashboard → Orders: the order with client id `live-SPY-<date>-entry`, status filled, ~$25 notional.
`python -m bot status --live` shows the position.

## 16. Reconcile
`python -m bot risk show --run-id live` → `positions.SPY.qty` equals the broker's quantity.

## 17. Restart the bot
Start a new `trade --live --once` (it starts DISARMED; the arm file is deleted on start). Expected: `interlock
blocked` is **not** reached because nothing needs submitting; decision `none` ("target matches position").

## 18. Reconcile again
`status --live` and `risk show --run-id live` agree with the dashboard. `logs/decisions_live.jsonl` shows no
second submit for the same session.

## 19. Test controlled exit
Re-arm (`live arm`). Either wait for the strategy's exit signal (may take weeks) or, for the validation, close the
position from the Alpaca dashboard and run one more cycle: expected a `reconciliation: SPY missing at broker`
warning and a clean local state. (Closing at the broker rather than in the bot exercises the reconciliation path,
which is the one that matters when something goes wrong.)

## 20. Disable live automation
```
python -m bot live disarm
```
Confirm `LIVE_AUTONOMOUS_TRADING=false` in `.env` (never set it to true during this runbook).

## 21. Review logs
```
python -m bot review --run-id live
```
Read: fills, realized slippage vs the 3 bps assumption, blocked decisions, exceptions. File anything surprising as
an issue before considering the constrained-autonomous phase.

## Aborting at any step
- `python -m bot live disarm`, then if a position is open and you want out: close it in the Alpaca dashboard.
- Kill switch tripped: it liquidated everything and refuses to trade. Investigate, then `python -m bot risk reset --run-id live`.
- Suspect a key leak: rotate in the dashboard immediately; everything else can wait.

---

## V1.5 additions (Phase 4)

**Policy fingerprint.** `python -m bot live arm` loads `config/policy.live.yaml` with the promotion check, prints its
SHA-256 fingerprint and stores it in the arm file. `live check` and the daemon compare the loaded policy with the
armed fingerprint; any edit to the policy file disarms (gate `policy_fingerprint_matches`). No module in the live
policy may lack a line in `research/PROMOTIONS.md` (gate `live_modules_promoted`); today nothing is promoted, so
arming refuses. That is the intended state until the research protocol completes on the sealed cut D.

**PDT check.** `policy.pdt_mode` must match the account: `legacy_guard` (sub-$25k, the RiskEngine refuses an M2 intent
that would create a 4th day trade in 5 sessions from `AccountInfo.daytrade_count` and its own ledger), `intraday_margin`
(verified $25k+), or `none` (M2 excluded). Verify from `python -m bot status --live` before editing the policy.

**Protective-order policy.** Whole-share overnight entries carry an OTO stop leg; auction (OPG/CLS) entries get a
standalone GTC stop on fill; fractional positions get a DAY stop re-placed at every pre-open and are reported as
`unprotected_overnight`; a rejected protective order marks the slice `unprotected`, alerts, and flattens at the next
eligible time when `require_broker_protection_overnight` is true. An exit or flatten cancels the protective order first.

**State.** The runtime persists to `state/<run-id>.sqlite` (WAL). `python -m bot state migrate --run-id paper` imports the
V1 JSON file once; `state show` summarises; `risk unthrottle --module M2` is the only way to restore a halved budget.
`python -m bot gates` prints the paper→live candidate gates from the store; `gates --live` the step-up gates. Both are
read-only.
