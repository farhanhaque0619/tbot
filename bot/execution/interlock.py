"""Live-trading interlock (Phase 4) and arm/disarm state.

Gates that must ALL pass before a live order can be submitted (checked again before every order):
 1. live credentials exist            6. trading_blocked is false
 2. TRADING_ENV=live                  7. account_blocked is false
 3. CLI --live flag                   8. market data fresh
 4. operator typed the confirmation   9. risk manager healthy (no halt, no last error)
 5. account is not the paper account 10. kill switch not engaged
plus: the bot is ARMED (``python -m bot live arm``), the arm has not expired, and the SAFE limits acknowledged at
arm time are still the configured ones. Arm state lives in ``state/live.armed.json`` and is cleared on every
process start, so the default state after any restart is DISARMED.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.config import LIVE_CONFIRMATION_PHRASE, Settings
from bot.risk.manager import SafeLiveLimits


@dataclass(frozen=True)
class Gate:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ArmState:
    armed_at: str
    expires_at: str
    safe_fingerprint: str
    confirmed_phrase: bool
    operator: str = ""
    policy_fingerprint: str = ""          # SHA-256 of the armed RiskPolicy (V1.5); "" for a pre-V1.5 arm file

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(self.expires_at)


class LiveInterlock:
    ARM_FILE = "live.armed.json"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = Path(settings.state_dir) / self.ARM_FILE
        self.safe = SafeLiveLimits.from_settings(settings) if settings.safe_live_test_mode else None

    # ------------------------------------------------------------- arm state
    def arm(self, *, typed_phrase: str, acknowledged: bool, ttl_minutes: int | None = None, policy_fingerprint: str = "") -> ArmState:
        if typed_phrase.strip() != LIVE_CONFIRMATION_PHRASE:
            raise PermissionError("confirmation phrase did not match; not armed")
        if not acknowledged:
            raise PermissionError("safe-mode configuration not acknowledged; not armed")
        if not self.settings.has_credentials("live"):
            raise PermissionError("live credentials are not configured; not armed")
        if self.settings.trading_env != "live":
            raise PermissionError("TRADING_ENV is not 'live'; not armed")
        ttl = ttl_minutes or self.settings.live_arm_ttl_minutes
        now = datetime.now(timezone.utc)
        st = ArmState(now.isoformat(), (now + timedelta(minutes=ttl)).isoformat(),
                      self.safe.fingerprint() if self.safe else "unsafe-mode", True, os.environ.get("USER", ""), policy_fingerprint)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(st.__dict__, indent=2))
        return st

    def disarm(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False

    def clear_on_startup(self) -> None:
        """Default startup state is DISARMED."""
        self.disarm()

    def arm_state(self) -> ArmState | None:
        if not self.path.exists():
            return None
        try:
            return ArmState(**json.loads(self.path.read_text()))
        except (ValueError, TypeError):
            return None

    def is_armed(self, *, policy_fingerprint: str | None = None) -> tuple[bool, str]:
        """Armed, not expired, safe limits unchanged and (when a policy fingerprint is given) the same policy as armed."""
        st = self.arm_state()
        if st is None:
            return False, "not armed (run: python -m bot live arm)"
        if st.expired:
            return False, f"arm expired at {st.expires_at}"
        current = self.safe.fingerprint() if self.safe else "unsafe-mode"
        if st.safe_fingerprint != current:
            return False, "safe-mode limits changed since arming; re-arm required"
        if not st.confirmed_phrase:
            return False, "confirmation phrase missing"
        if policy_fingerprint is not None:
            if not st.policy_fingerprint:
                return False, "armed without a policy fingerprint; re-arm with the current policy"
            if st.policy_fingerprint != policy_fingerprint:
                return False, f"policy changed since arming (armed {st.policy_fingerprint[:12]}…, loaded {policy_fingerprint[:12]}…); re-arm required"
        return True, f"armed until {st.expires_at}" + (f" with policy {st.policy_fingerprint[:12]}…" if st.policy_fingerprint else "")

    # ------------------------------------------------------------------ gates
    def check(self, *, cli_live_flag: bool, account=None, account_env_ok: tuple[bool, str] | None = None,
              data_fresh: bool | None = None, risk_manager=None, risk_last_error: str | None = None,
              require_armed: bool = True, policy=None, promotions_path=None) -> list[Gate]:
        s = self.settings
        gates = [
            Gate("live_credentials_present", s.has_credentials("live"), "ALPACA_LIVE_API_KEY / ALPACA_LIVE_SECRET_KEY"),
            Gate("trading_env_is_live", s.trading_env == "live", f"TRADING_ENV={s.trading_env}"),
            Gate("cli_live_flag", cli_live_flag, "--live"),
        ]
        fp = policy.fingerprint() if policy is not None else None
        armed, why = self.is_armed(policy_fingerprint=fp)
        gates.append(Gate("operator_confirmation_and_armed", armed if require_armed else True, why))
        if policy is not None:
            st = self.arm_state()
            gates.append(Gate("policy_fingerprint_matches", bool(st and st.policy_fingerprint == fp), f"loaded {fp[:12]}…"))
            from bot.core.policy import PROMOTIONS_PATH
            bad = policy.unpromoted_modules(promotions_path or PROMOTIONS_PATH)
            gates.append(Gate("live_modules_promoted", not bad, "all promoted" if not bad else f"unpromoted: {bad} (research/PROMOTIONS.md)"))
        if account_env_ok is not None:
            gates.append(Gate("account_is_not_paper", account_env_ok[0], account_env_ok[1]))
        else:
            gates.append(Gate("account_is_not_paper", False, "account not queried"))
        if account is not None:
            gates.append(Gate("trading_not_blocked", not account.trading_blocked, f"trading_blocked={account.trading_blocked}"))
            gates.append(Gate("account_not_blocked", not account.account_blocked and account.status.upper() == "ACTIVE",
                              f"account_blocked={account.account_blocked} status={account.status}"))
        else:
            gates.append(Gate("trading_not_blocked", False, "account not queried"))
            gates.append(Gate("account_not_blocked", False, "account not queried"))
        gates.append(Gate("market_data_fresh", bool(data_fresh), "" if data_fresh else "stale or unknown"))
        healthy = risk_manager is not None and not risk_manager.halted_today and not risk_last_error
        gates.append(Gate("risk_manager_healthy", healthy, risk_last_error or ("halted today" if risk_manager is not None and risk_manager.halted_today else "")))
        gates.append(Gate("kill_switch_clear", risk_manager is not None and not risk_manager.killed,
                          getattr(getattr(risk_manager, "state", None), "kill_reason", "")))
        gates.append(Gate("safe_live_test_mode", s.safe_live_test_mode or True,
                          "ON" if s.safe_live_test_mode else "OFF (operator disabled; not recommended)"))
        return gates

    @staticmethod
    def all_pass(gates: list[Gate]) -> bool:
        return all(g.ok for g in gates)
