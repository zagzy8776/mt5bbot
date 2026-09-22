"""Normalized domain events and trading contracts.

Signals are not orders. Orders are not executions until broker-reconciled.
"""

from __future__ import annotations

from collections.abc import Iterable
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
        return value.strip()


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
        return value.strip()


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
        return value.strip()


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
    magic: int | None = None
    comment: str = ""
    is_external: bool = False


class AuditEvent(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    component: str
    event_type: str
    severity: Severity = Severity.INFO
    symbol: str | None = None
    correlation_id: str = Field(default_factory=new_correlation_id)
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class NewsEvent(BaseModel):
    """One macroeconomic/calendar event, as a producer actually reported it.

    Provenance first: the provider, the source label and the times are stored as given. Fields the
    provider does not supply stay ``None`` — nothing is inferred or back-filled.
    """

    event_id: str = Field(default_factory=new_correlation_id)
    dedup_key: str = ""  # stable identity so a re-fetch cannot duplicate a row
    published_at: datetime  # when the event is scheduled/published (UTC)
    fetched_at: datetime = Field(default_factory=utc_now)
    source: str = ""  # provider label, e.g. "file:news_events.json"
    provider: str = ""  # file | http | static | manual
    currencies: list[str] = Field(default_factory=list)  # e.g. ["USD"]
    country: str = ""
    title: str = ""
    impact: str = ""  # high | medium | low | unknown
    event_type: str = ""
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def affects(self, currencies: Iterable[str]) -> bool:
        wanted = {c.strip().upper() for c in currencies if c and c.strip()}
        if not wanted:
            return False
        return bool({c.strip().upper() for c in self.currencies} & wanted)


class ResearchNote(BaseModel):
    """A structured finding from an autonomous research run, with its provenance.

    Research output is evidence with a source, never an instruction: a note cannot change
    configuration, and it carries the URL, fetch time, provider and content hash it came from.
    """

    note_id: str = Field(default_factory=new_correlation_id)
    url: str = ""
    domain: str = ""
    title: str = ""
    summary: str = ""
    text_excerpt: str = ""
    tags: list[str] = Field(default_factory=list)
    source_kind: str = ""  # intel | research | documentation
    content_hash: str = ""
    fetched_at: datetime = Field(default_factory=utc_now)
    provenance: dict[str, Any] = Field(default_factory=dict)


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
