"""Shared enumerations for platform-wide state machines."""

from __future__ import annotations

from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    """Order lifecycle. Transition only via OrderManager reconciliation."""

    CREATED = "created"
    RISK_CHECK = "risk_check"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    BROKER_REJECTED = "broker_rejected"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ProxyState(StrEnum):
    HEALTHY = "healthy"
    IN_USE = "in_use"
    COOLING = "cooling"
    QUARANTINED = "quarantined"


class ProxyErrorType(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    PROXY_HANDSHAKE = "proxy_handshake"
    NETWORK_LATENCY = "network_latency"
    PACKET_LOSS = "packet_loss"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    UNKNOWN = "unknown"


class AuditEventType(StrEnum):
    DATA_RECEIVED = "DATA_RECEIVED"
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    THESIS_EMITTED = "THESIS_EMITTED"
    THESIS_REJECTED = "THESIS_REJECTED"
    RISK_CHECK_FAILED = "RISK_CHECK_FAILED"
    RISK_APPROVED = "RISK_APPROVED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_TRANSITION_REJECTED = "ORDER_TRANSITION_REJECTED"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    AGENT_ERROR = "AGENT_ERROR"
    ORDER_FILLED = "ORDER_FILLED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_MODIFIED = "POSITION_MODIFIED"
    POSITION_CLOSED = "POSITION_CLOSED"
    MT5_DISCONNECTED = "MT5_DISCONNECTED"
    PROXY_FAILED = "PROXY_FAILED"
    SCRAPER_FAILED = "SCRAPER_FAILED"
    STRATEGY_ERROR = "STRATEGY_ERROR"
    STRATEGY_ENABLED = "STRATEGY_ENABLED"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    STRATEGY_CONFIG_INVALID = "STRATEGY_CONFIG_INVALID"
    SINK_FAILED = "SINK_FAILED"
    RISK_KILL_SWITCH_RELEASED = "RISK_KILL_SWITCH_RELEASED"
    RISK_TRADING_PAUSED = "RISK_TRADING_PAUSED"
    RISK_TRADING_RESUMED = "RISK_TRADING_RESUMED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    SYSTEM_START = "SYSTEM_START"
    SYSTEM_STOP = "SYSTEM_STOP"
    HEALTH_CHECK = "HEALTH_CHECK"


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ComponentHealth(StrEnum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    DISABLED = "disabled"


class RegimeLabel(StrEnum):
    """Market regime. Every label must be backed by measurable evidence."""

    TRENDING = "trending"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    BREAKOUT = "breakout"
    TRANSITION = "transition"
    ABNORMAL = "abnormal"
    UNDEFINED = "undefined"  # insufficient evidence to classify


class DataQualityLevel(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    CRITICAL = "critical"


class EvidenceQuality(StrEnum):
    """Quality grade for historical evidence samples.

    A sample is INSUFFICIENT until it crosses a minimum size threshold.
    The system must never present a 4-trade sample as strong evidence.
    """

    INSUFFICIENT = "insufficient"
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class TradeCause(StrEnum):
    """Why a trade exited. Used for cause_class breakdown."""

    TARGET_HIT = "target_hit"
    STOP_HIT = "stop_hit"
    TIME_EXIT = "time_exit"
    MANUAL = "manual"
    REGIME_CHANGE = "regime_change"
    DATA_DEGRADED = "data_degraded"
    KILL_SWITCH = "kill_switch"
    UNKNOWN = "unknown"
