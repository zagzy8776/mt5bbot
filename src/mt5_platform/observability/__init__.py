"""Observability helpers: health payloads and structured logging setup."""

from __future__ import annotations

from typing import Any

import structlog

from mt5_platform.common.enums import ComponentHealth
from mt5_platform.config import Settings


def configure_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), level.upper(), 20)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def build_health_payload(
    settings: Settings,
    *,
    components: dict[str, ComponentHealth] | None = None,
) -> dict[str, Any]:
    components = components or {
        "api": ComponentHealth.UP,
        "ingestion": ComponentHealth.DISABLED
        if not settings.ingestion_enabled
        else ComponentHealth.DOWN,
        "storage": ComponentHealth.DISABLED,
        "strategy": ComponentHealth.UP,
        "risk": ComponentHealth.UP,
        "execution": ComponentHealth.DISABLED,
        "mt5": ComponentHealth.DISABLED,
    }
    overall = ComponentHealth.UP
    if any(v is ComponentHealth.DOWN for v in components.values()):
        overall = ComponentHealth.DEGRADED

    return {
        "status": overall.value,
        "app": settings.app_name,
        "env": settings.app_env,
        "trading_mode": settings.trading_mode.value,
        "is_live": settings.is_live,
        "kill_switch": settings.emergency_kill_switch,
        "default_symbol": settings.default_symbol,
        "components": {k: v.value for k, v in components.items()},
    }
