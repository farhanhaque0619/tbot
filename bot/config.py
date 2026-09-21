"""All settings come from environment variables (or a local .env file).

Secrets are ``SecretStr`` so they never show up in ``repr()``/logs by accident.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

NY_TZ = "America/New_York"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Alpaca ------------------------------------------------------------
    alpaca_api_key: SecretStr = Field(default=SecretStr(""))
    alpaca_secret_key: SecretStr = Field(default=SecretStr(""))
    alpaca_paper_url: str = "https://paper-api.alpaca.markets"

    # --- Safety ------------------------------------------------------------
    live_trading: bool = False  # real money. Off. Stays off unless you fight three safeties.

    # --- Data --------------------------------------------------------------
    data_feed: Literal["sip", "iex"] = "sip"
    data_adjustment: Literal["raw", "split", "dividend", "all"] = "split"
    data_db_path: Path = Path("data_cache/bars.duckdb")

    # --- Risk --------------------------------------------------------------
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.1)
    max_position_pct: float = Field(default=0.50, gt=0, le=1.0)
    daily_loss_limit_pct: float = Field(default=0.03, gt=0, le=0.5)
    max_drawdown_pct: float = Field(default=0.20, gt=0, le=0.9)
    max_positions: int = Field(default=5, ge=1)
    atr_stop_mult: float = Field(default=2.0, gt=0)

    # --- Costs (backtest) --------------------------------------------------
    slippage_bps: float = Field(default=2.0, ge=0)
    spread_bps: float = Field(default=2.0, ge=0)
    commission_per_share: float = Field(default=0.0, ge=0)

    # --- Execution ---------------------------------------------------------
    order_time_in_force: Literal["opg", "day"] = "opg"
    poll_interval_seconds: int = Field(default=300, ge=5)
    state_dir: Path = Path("state")

    # --- Monitoring --------------------------------------------------------
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    discord_webhook_url: SecretStr = Field(default=SecretStr(""))

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # --- helpers -----------------------------------------------------------
    @property
    def has_alpaca_keys(self) -> bool:
        return bool(self.alpaca_api_key.get_secret_value() and self.alpaca_secret_key.get_secret_value())

    @property
    def paper(self) -> bool:
        """True unless live trading is enabled. Callers must ALSO pass the CLI gate."""
        return not self.live_trading

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs (used by the log redaction filter)."""
        return [
            s for s in (
                self.alpaca_api_key.get_secret_value(),
                self.alpaca_secret_key.get_secret_value(),
                self.discord_webhook_url.get_secret_value(),
            ) if s
        ]


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
