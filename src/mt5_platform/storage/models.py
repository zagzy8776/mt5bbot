"""SQLAlchemy ORM models for time-series and trading artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class TickRow(Base):
    __tablename__ = "ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class CandleRow(Base):
    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), index=True, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tick_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spread_avg: Mapped[float | None] = mapped_column(Float, nullable=True)


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class ExecutionRow(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    order_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    requested_volume: Mapped[float] = mapped_column(Float, nullable=False)
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    mt5_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    execution_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="open")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class AccountSnapshotRow(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    free_margin: Mapped[float] = mapped_column(Float, nullable=False)
    used_margin: Mapped[float] = mapped_column(Float, nullable=False)
    margin_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    floating_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    daily_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exposure: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class AuditRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    component: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutcomeRow(Base):
    """Recorded trade outcome (the live/backtest ledger).

    Typed columns exist for the indexes the evidence engine and dashboard query on; the complete
    ``HistoricalOutcome`` is also stored as JSON so nothing is lost in the round trip.
    """

    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="closed")
    source: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, default="autonomous"
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), index=True, nullable=False, default="")
    strategy: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="")
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    broker_ticket: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    remaining_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_time_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_cause: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    exit_cause_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cause_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thesis_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    context_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradeLegRow(Base):
    """One execution leg of a trade (entry, partial exit, final exit)."""

    __tablename__ = "trade_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    leg_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    trade_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_cause: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    broker_deal: Mapped[str | None] = mapped_column(String(64), nullable=True)
