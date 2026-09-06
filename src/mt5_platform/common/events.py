"""Normalized domain events and trading contracts.

Signals are not orders. Orders are not executions until broker-reconciled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from mt5_platform.common.enums import OrderSide, OrderStatus, Severity
from mt5_platform.common.ids import new_correlation_id, new_order_id, new_signal_id


def utc_now() -> datetime:
    return datetime.now(UTC)


class MarketDataEvent(BaseModel):
    """Normalized market tick / quote from any authorized source."""

    timestamp: datetime
    source: str
    symbol: str
    bid: float | None = None
    ask: float | None = None
    price: float | None = None
    volume: float | None = None
    spread: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = Field(default_factory=new_correlation_id)

    @field_validator("timestamp")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class StrategySignal(BaseModel):
    """Structured trading intent. Must pass RiskEngine before any order."""

    signal_id: str = Field(default_factory=new_signal_id)
    symbol: str
    direction: OrderSide
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reason: str = ""
    timestamp: datetime = Field(default_factory=utc_now)
    strategy_name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = Field(default_factory=new_correlation_id)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)
    correlation_id: str = Field(default_factory=new_correlation_id)


class OrderRequest(BaseModel):
    """Risk-approved order intent destined for OrderManager."""

    order_id: str = Field(default_factory=new_order_id)
    signal_id: str | None = None
    symbol: str
    side: OrderSide
    volume: float
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    status: OrderStatus = OrderStatus.CREATED
    filled_volume: float | None = None
    rejection_reason: str | None = None
    correlation_id: str = Field(default_factory=new_correlation_id)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class ExecutionRecord(BaseModel):
    """Broker interaction outcome. Local success ≠ broker fill until reconciled."""

    execution_id: str
    order_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    symbol: str
    side: OrderSide
    requested_volume: float
    requested_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    mt5_response: dict[str, Any] = Field(default_factory=dict)
    execution_price: float | None = None
    filled_volume: float | None = None
    slippage: float | None = None
    rejection_reason: str | None = None
    final_status: OrderStatus
    correlation_id: str = Field(default_factory=new_correlation_id)


class PositionInfo(BaseModel):
    """Broker-side open position (simulated by the mock adapter)."""

    ticket: str
    order_id: str | None = None
    symbol: str
    side: OrderSide
    volume: float
    entry_price: float
    current_price: float | None = None
    floating_pnl: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    opened_at: datetime = Field(default_factory=utc_now)


class AuditEvent(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    component: str
    event_type: str
    severity: Severity = Severity.INFO
    symbol: str | None = None
    correlation_id: str = Field(default_factory=new_correlation_id)
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class AccountSnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    balance: float
    equity: float
    free_margin: float
    used_margin: float
    margin_level: float | None = None
    floating_pnl: float
    daily_pnl: float = 0.0
    drawdown_pct: float = 0.0
    open_positions: int = 0
    exposure: float = 0.0
