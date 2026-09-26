"""All settings come from environment variables (or a local .env file).

Credential model (Phase 1):
- TRADING_ENV selects "paper" (default) or "live".
- Paper and live have SEPARATE key pairs: ALPACA_PAPER_API_KEY/SECRET and ALPACA_LIVE_API_KEY/SECRET.
  There is deliberately no generic ALPACA_API_KEY.
- Secrets are pydantic ``SecretStr`` so they never show up in repr()/logs; ``secret_values()`` feeds the
  log redaction filter. Nothing in this module prints a secret.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

NY_TZ = "America/New_York"
TradingEnv = Literal["paper", "live"]

PAPER_KEY_PREFIX = "PK"   # Alpaca paper key ids start with PK, live key ids with AK (heuristic, see STRICT_KEY_PREFIX_CHECK)
LIVE_KEY_PREFIX = "AK"
LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND THIS USES REAL MONEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # --- Environment / credentials ------------------------------------------------------------
    trading_env: TradingEnv = "paper"
    alpaca_paper_api_key: SecretStr = Field(default=SecretStr(""))
    alpaca_paper_secret_key: SecretStr = Field(default=SecretStr(""))
    alpaca_live_api_key: SecretStr = Field(default=SecretStr(""))
    alpaca_live_secret_key: SecretStr = Field(default=SecretStr(""))
    strict_key_prefix_check: bool = True     # refuse a PK… key in the live slot or an AK… key in the paper slot

    # --- Live safety ---------------------------------------------------------------------------
    live_autonomous_trading: bool = False    # multi-cycle live loop. False = only single --once cycles while armed.
    live_arm_ttl_minutes: int = Field(default=30, ge=1, le=240)
    safe_live_test_mode: bool = True         # Phase 5 constraints, see SafeLiveLimits.from_settings
    safe_max_order_notional: float = Field(default=25.0, gt=0)
    safe_max_gross_exposure: float = Field(default=50.0, gt=0)
    safe_max_daily_loss: float = Field(default=5.0, gt=0)         # dollars
    safe_max_account_drawdown: float = Field(default=10.0, gt=0)  # dollars, from peak equity
    safe_max_positions: int = Field(default=1, ge=1)
    safe_allowed_symbols: str = ""           # comma-separated allow-list; empty = any fractionable, non-marginable-dependent equity

    # --- Data ---------------------------------------------------------------------------------
    data_feed: Literal["sip", "iex"] = "sip"
    data_adjustment: Literal["raw", "split", "dividend", "all"] = "split"
    data_db_path: Path = Path("data_cache/bars.duckdb")
    data_plan: Literal["basic", "plus"] = "basic"     # basic: 1 WS connection, 30 symbols, SIP lagged 15 min
    universe_path: Path = Path("config/universe.yaml")
    max_stale_data_seconds: int = Field(default=900, ge=0)   # freshness threshold for quotes/account snapshots

    # --- Risk (shared by backtest and execution) ----------------------------------------------
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.1)
    max_position_pct: float = Field(default=0.50, gt=0, le=1.0)
    daily_loss_limit_pct: float = Field(default=0.03, gt=0, le=0.5)
    max_drawdown_pct: float = Field(default=0.20, gt=0, le=0.9)
    max_positions: int = Field(default=5, ge=1)
    atr_stop_mult: float = Field(default=2.0, gt=0)
    allow_fractional: bool = True
    qty_decimals: int = Field(default=3, ge=0, le=9)
    max_spread_bps: float = Field(default=50.0, gt=0)          # price/spread sanity gate
    max_price_deviation_pct: float = Field(default=0.10, gt=0)  # quote vs last close sanity gate

    # --- Costs (backtest) ---------------------------------------------------------------------
    slippage_bps: float = Field(default=2.0, ge=0)
    spread_bps: float = Field(default=2.0, ge=0)
    commission_per_share: float = Field(default=0.0, ge=0)

    # --- Execution ----------------------------------------------------------------------------
    order_time_in_force: Literal["opg", "day"] = "opg"
    poll_interval_seconds: int = Field(default=300, ge=5)
    state_dir: Path = Path("state")
    paper_policy_path: Path = Path("config/policy.paper.yaml")
    live_policy_path: Path = Path("config/policy.live.yaml")

    # --- Research-only feature flags (never affect execution) ---------------------------------
    enable_jev: bool = False
    jev_endpoint: str = ""
    jev_api_key: SecretStr = Field(default=SecretStr(""))
    enable_kelly: bool = False

    # --- Monitoring ---------------------------------------------------------------------------
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    discord_webhook_url: SecretStr = Field(default=SecretStr(""))

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # --- credential helpers (never return secrets to callers that print) ----------------------
    def _pair(self, env: TradingEnv) -> tuple[SecretStr, SecretStr]:
        if env == "paper":
            return self.alpaca_paper_api_key, self.alpaca_paper_secret_key
        if env == "live":
            return self.alpaca_live_api_key, self.alpaca_live_secret_key
        raise ValueError(f"unknown trading env {env!r}")

    def has_credentials(self, env: TradingEnv) -> bool:
        k, s = self._pair(env)
        return bool(k.get_secret_value() and s.get_secret_value())

    def credential_status(self, env: TradingEnv) -> dict[str, bool | str]:
        """Presence and shape only. Never the values."""
        k, s = self._pair(env)
        kv = k.get_secret_value()
        prefix_ok = True
        if kv and self.strict_key_prefix_check:
            expected = PAPER_KEY_PREFIX if env == "paper" else LIVE_KEY_PREFIX
            prefix_ok = kv.startswith(expected)
        return {"env": env, "key_present": bool(kv), "secret_present": bool(s.get_secret_value()),
                "key_prefix_ok": prefix_ok, "key_prefix": kv[:2] if kv else ""}

    def credentials(self, env: TradingEnv) -> tuple[str, str]:
        """Return (key, secret) for ``env`` or raise. Enforces the paper/live prefix heuristic."""
        k, s = self._pair(env)
        kv, sv = k.get_secret_value(), s.get_secret_value()
        if not kv or not sv:
            raise RuntimeError(f"ALPACA_{env.upper()}_API_KEY / ALPACA_{env.upper()}_SECRET_KEY are not set (see .env.example)")
        if self.strict_key_prefix_check:
            if env == "paper" and kv.startswith(LIVE_KEY_PREFIX):
                raise RuntimeError("ALPACA_PAPER_API_KEY looks like a LIVE key (starts with AK). Refusing. "
                                   "Put live keys in ALPACA_LIVE_API_KEY. (Set STRICT_KEY_PREFIX_CHECK=false only if you are sure.)")
            if env == "live" and kv.startswith(PAPER_KEY_PREFIX):
                raise RuntimeError("ALPACA_LIVE_API_KEY looks like a PAPER key (starts with PK). Refusing.")
        return kv, sv

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs (used by the log redaction filter)."""
        vals = [self.alpaca_paper_api_key, self.alpaca_paper_secret_key, self.alpaca_live_api_key,
                self.alpaca_live_secret_key, self.discord_webhook_url, self.jev_api_key]
        return [v.get_secret_value() for v in vals if v.get_secret_value()]

    @property
    def is_live(self) -> bool:
        return self.trading_env == "live"


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings


def banner(env: TradingEnv) -> str:
    if env == "live":
        line = "!" * 48
        return f"{line}\nTRADING ENVIRONMENT: LIVE — REAL MONEY\n{line}"
    line = "=" * 48
    return f"{line}\nTRADING ENVIRONMENT: PAPER\n{line}"
