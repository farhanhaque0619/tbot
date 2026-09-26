# Deployment (V1.5, paper first)

## Why a small VPS and not a laptop

A laptop sleeps, updates, changes networks and loses Wi‑Fi at 15:29. The daemon holds one market-data websocket and
the trade-updates stream, schedules auction orders to the minute, and must be reachable by the watchdog. A 1‑vCPU /
2 GB Ubuntu 24.04 VPS in a US‑East region (close to Alpaca's endpoints; latency matters little for this design but
jitter and reliability do) costs a few dollars a month and stays up. Docker is optional: two systemd units and a venv
are simpler to inspect, and the whole process is a single Python interpreter with no services beside it.

## Host setup (once)

```
sudo apt update && sudo apt install -y python3.11 python3.11-venv git chrony
sudo timedatectl set-timezone UTC
sudo useradd --system --create-home --home-dir /opt/tbot --shell /usr/sbin/nologin tbot
sudo mkdir -p /etc/tbot && sudo touch /etc/tbot/env && sudo chown root:tbot /etc/tbot/env && sudo chmod 640 /etc/tbot/env
```

`/etc/tbot/env` holds the environment (never commit it, never paste it into chat):

```
TRADING_ENV=paper
ALPACA_PAPER_API_KEY=...
ALPACA_PAPER_SECRET_KEY=...
DATA_PLAN=basic
DISCORD_WEBHOOK_URL=...
LIVE_AUTONOMOUS_TRADING=false
SAFE_LIVE_TEST_MODE=true
```

Live keys are added only when the live gates pass (`python -m bot gates`), as `ALPACA_LIVE_*`, and the file stays 640.
chrony keeps the clock within milliseconds: the scheduler's 15:50 CLS cutoff and the 09:28 OPG cutoff depend on it.

## Install a tagged release

```
sudo -u tbot -H bash -c '
  cd /opt/tbot && git clone https://github.com/farhanhaque0619/tbot.git . && git checkout v1.5-phase6-paper-start
  python3.11 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt
  mkdir -p state logs data_cache reports'
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tbot tbot-watchdog
```

Before the first start, on the same box: `python -m bot doctor --paper`, `python -m bot data fetch --symbol SPY --symbol QQQ
--start 2014-01-01 --end <today>` (daily warm-up), `python -m bot state migrate --run-id paper` if a V1 JSON state exists,
and `python -m bot run --env paper --once` to see the boot, reconcile and one cycle without streams.

## Operations

- Logs: `journalctl -u tbot -f`, `journalctl -u tbot-watchdog -f`; JSONL in `logs/` rotate by size (see `bot/monitoring/logging.py`).
- Stop: `systemctl stop tbot` sends SIGTERM; the daemon finishes the cycle, cancels non-protective open entries, keeps
  protective stops, persists `state/paper.sqlite` and exits 0. A crash exits non-zero and systemd restarts it after 10 s,
  at most 5 times in 10 minutes; then it stays down and the watchdog halts entries and alerts.
- Watchdog: `config/watchdog.yaml` decides what it may do; by default alert → halt entries (`state/paper.halt`) →
  cancel pending entries → flatten on `heartbeat_stale_300s` / `drawdown_breach`. Clearing the halt is automatic when the
  checks are clean again; the flag can also be removed by hand.
- Nightly snapshot (cron, as `tbot`): `sqlite3 state/paper.sqlite ".backup state/backup/paper-$(date +%F).sqlite"` plus
  `tar czf backup/logs-$(date +%F).tgz logs/`. Keep 30 days.
- Upgrades: `git fetch && git checkout <tag>`, `pip install -r requirements.txt`, `python -m pytest -q`, `systemctl restart tbot`.
  Every restart disarms live; the policy fingerprint is re-checked on boot.

## Live (later, not now)

Live needs, in this order: every module in `config/policy.live.yaml` promoted (`research/PROMOTIONS.md`), `python -m bot gates`
all PASS, `LIVE_AUTONOMOUS_TRADING=true` in the env file, `TRADING_ENV=live`, `ALPACA_LIVE_*` keys, a second unit
`tbot-live.service` with `ExecStart=... -m bot run --env live --live`, and `python -m bot live arm` in a terminal after every
start (the arm expires and every restart disarms). `SAFE_LIVE_TEST_MODE=true` keeps the dollar caps on top of the policy.
