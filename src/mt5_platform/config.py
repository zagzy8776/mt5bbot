"""Application configuration with hard demo/live trading gates."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    """Execution mode. LIVE is gated and must never be the accidental default."""

    DEMO = "demo"
    LIVE = "live"


class Settings(BaseSettings):
    """Central settings loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "mt5-platform"
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    trading_mode: TradingMode = TradingMode.DEMO
    live_trading_enabled: bool = False
    live_trading_acknowledged: bool = False

    default_symbol: str = "XAUUSD"

    max_position_size: float = 0.10
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    max_simultaneous_positions: int = 3
    max_spread_points: float = 50.0
    max_slippage_points: float = 25.0
    max_exposure_pct: float = 1000.0
    min_margin_level_pct: float = 20.0
    stale_data_max_age_ms: int = 5000

    emergency_kill_switch: bool = False

    ingestion_workers: int = Field(default=4, ge=1, le=64)
    ingestion_enabled: bool = False
    ingestion_stale_context_s: float = 300.0
    proxy_cooldown_s: float = 30.0
    proxy_quarantine_s: float = 300.0
    resource_filter_calibrated: bool = False

    storage_backend: str = "memory"  # memory | sqlite | postgres
    database_url: str = "postgresql+asyncpg://mt5:mt5@127.0.0.1:5432/mt5_platform"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # Strategy / signal engine (Phase 4)
    # Comma-separated strategy names from the registry: null | sma_crossover |
    # breakout | mean_reversion | momentum
    strategies: str = "sma_crossover,breakout"
    signal_min_confidence: float = 0.0
    signal_require_stop_loss: bool = True
    signal_cooldown_s: float = 0.0
    signal_store_sink_enabled: bool = True
    signal_audit_store_sink_enabled: bool = True

    mt5_terminal_path: str = ""
    mt5_login: str = ""
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_timeout_ms: int = 10_000

    # Order / execution engine (Phase 6). "mt5" backend is Phase 7 (demo only).
    execution_backend: str = "mock"  # mock | mt5
    mock_starting_balance: float = 10_000.0
    mock_slippage: float = 0.0  # simulated price slippage per fill
    mock_fill_ratio: float = 1.0  # < 1.0 simulates partial fills
    order_store_sink_enabled: bool = True

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _enforce_live_gates(self) -> Settings:
        """LIVE mode requires explicit dual acknowledgment. Never soft-fail into live."""
        if self.trading_mode is TradingMode.LIVE:
            if not (self.live_trading_enabled and self.live_trading_acknowledged):
                raise ValueError(
                    "TradingMode.LIVE requires LIVE_TRADING_ENABLED=true and "
                    "LIVE_TRADING_ACKNOWLEDGED=true. Refusing to start in live mode."
                )
        return self

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    @property
    def is_demo(self) -> bool:
        return self.trading_mode is TradingMode.DEMO

    @property
    def trading_allowed(self) -> bool:
        """New orders may only proceed when kill switch is off."""
        return not self.emergency_kill_switch

    @property
    def active_strategies(self) -> list[str]:
        """Normalized, de-duplicated strategy names from `strategies`."""
        names: list[str] = []
        for raw in self.strategies.split(","):
            name = raw.strip().lower()
            if name and name not in names:
                names.append(name)
        return names


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
