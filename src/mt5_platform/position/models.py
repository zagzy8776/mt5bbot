"""Position Intelligence models (Phase D).

Every position decision is traceable to the original thesis and current evidence.
The Position Manager never executes directly — it proposes, Risk validates,
Order Manager executes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from mt5_platform.common.enums import (
    InvalidationReason,
    OrderSide,
    PositionDecision,
    ThesisStatus,
)
from mt5_platform.common.ids import new_correlation_id, new_execution_id


class ThesisSnapshot(BaseModel):
    """Frozen snapshot of the original TradeThesis at position open.

    Captures the reasoning that led to the position so we can later
    compare against current market state.
    """

    thesis_id: str
    instrument: str
    direction: OrderSide
    regime: str | None = None
    confidence: float = 0.0
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    invalidation_levels: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    agent_opinions: list[dict[str, Any]] = Field(default_factory=list)


class PositionState(BaseModel):
    """Current state of an open position as seen by the system.

    This is the internal view — reconciliation with broker is separate.
    """

    position_id: str
    instrument: str
    direction: OrderSide
    volume: float
    entry_price: float
    current_price: float | None = None
    unrealized_pnl: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    opened_at: datetime
    last_update: datetime
    thesis_id: str
    thesis_snapshot: ThesisSnapshot | None = None


class PositionDecisionModel(BaseModel):
    """One position management decision with full traceability.

    The Position Manager produces this. It is NOT an order — it flows
    through RiskEngine -> OrderManager -> Execution.
    """

    decision_id: str = Field(default_factory=new_execution_id)
    position_id: str
    timestamp: datetime = Field(default_factory=lambda: __import__("mt5_platform.common.events", fromlist=["utc_now"]).utc_now())
    correlation_id: str = Field(default_factory=new_correlation_id)

    # Thesis traceability
    original_thesis_id: str
    current_context_id: str
    thesis_status: ThesisStatus = ThesisStatus.UNKNOWN

    # Decision
    decision: str = "hold"
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)

    # Evidence
    current_evidence: dict[str, Any] = Field(default_factory=dict)
    invalidation_state: dict[str, Any] = Field(default_factory=dict)

    # Proposed modifications (if any)
    proposed_stop_loss: float | None = None
    proposed_take_profit: float | None = None
    proposed_volume: float | None = None  # for REDUCE

    # Reasoning
    reason: str = ""
    risk_state: dict[str, Any] = Field(default_factory=dict)

    def is_actionable(self) -> bool:
        return self.decision in (
            "modify",
            "reduce",
            "exit",
            "emergency_exit",
        )


class PositionDecisionRequest(BaseModel):
    """Internal request to evaluate a position.

    Bundles everything the Position Manager needs for one evaluation cycle.
    """

    position: PositionState
    market_context: Any  # MarketContext
    historical_evidence: dict[str, Any] | None = None
    risk_context: dict[str, Any] | None = None


class PositionManagerConfig(BaseModel):
    """Configuration for the Position Manager."""

    # How far price must move beyond invalidation before thesis INVALIDATED
    invalidation_buffer_pct: float = 0.1

    # Max time (seconds) without context update before NO_ACTION
    max_stale_context_s: float = 300.0

    # Trailing stop parameters (if enabled)
    trail_activation_pct: float = 0.5  # % move in favor to activate
    trail_distance_atr_mult: float = 1.5

    # Partial reduction parameters
    reduce_volume_pct: float = 0.5  # fraction to reduce when weakening

    # Emergency conditions
    max_slippage_pct: float = 1.0
    kill_switch_check: bool = True


class PositionManagerAudit(BaseModel):
    """Audit record for one position evaluation cycle."""

    timestamp: datetime = Field(default_factory=lambda: __import__("mt5_platform.common.events", fromlist=["utc_now"]).utc_now())
    position_id: str
    thesis_id: str
    thesis_status: ThesisStatus
    decision: str
    confidence: float
    reason: str
    correlation_id: str
    invalidation_reasons: list[InvalidationReason] = Field(default_factory=list)


# Event types for audit
POSITION_DECISION_EMITTED = "POSITION_DECISION_EMITTED"
POSITION_THESIS_INVALIDATED = "POSITION_THESIS_INVALIDATED"
POSITION_RECONCILIATION_MISMATCH = "POSITION_RECONCILIATION_MISMATCH"
POSITION_EMERGENCY_TRIGGERED = "POSITION_EMERGENCY_TRIGGERED"