"""Learning models (Phase E).

All models are immutable facts where possible. Interpretations are
explicitly typed as hypotheses or counterfactuals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from mt5_platform.common.enums import (
    ConfigurationChangeType,
    LessonStatus,
    ReviewOutcome,
    TradeCause,
)


class TradeOutcome(BaseModel):
    """Frozen outcome of a completed trade — immutable facts only."""

    trade_id: str
    instrument: str
    strategy: str
    direction: str  # "buy" | "sell"
    entry: float
    exit: float | None = None
    stop_loss: float
    take_profit: float
    realized_pnl: float = 0.0
    return_pct: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    duration_s: float = 0.0
    exit_reason: TradeCause = TradeCause.UNKNOWN
    cause_class: TradeCause = TradeCause.UNKNOWN
    opened_at: datetime
    closed_at: datetime | None = None
    regime: str = ""
    context_id: str = ""
    thesis_id: str = ""
    order_id: str = ""
    execution_id: str = ""
    position_decisions: list[dict[str, Any]] = Field(default_factory=list)
    agent_opinions: list[dict[str, Any]] = Field(default_factory=list)
    thesis_snapshot: dict[str, Any] = Field(default_factory=dict)
    risk_decision: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()
    )


class PostTradeReview(BaseModel):
    """Structured review of a completed trade.

    Separates FACTS (immutable) from INTERPRETATION (hypothesis).
    """

    review_id: str = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.ids", fromlist=["new_execution_id"]
        ).new_execution_id()
    )
    trade_id: str
    thesis_id: str
    context_id: str

    # Expected (from entry-time beliefs)
    expected_direction: str = ""
    expected_regime: str = ""
    expected_thesis: str = ""
    expected_risk_pct: float = 0.0
    expected_target: float | None = None
    expected_invalidation: str = ""

    # Actual (immutable facts)
    actual_exit: float | None = None
    actual_pnl: float = 0.0
    actual_return_pct: float = 0.0
    actual_mae: float = 0.0
    actual_mfe: float = 0.0
    actual_duration_s: float = 0.0
    actual_exit_reason: str = ""

    # Divergence analysis
    direction_correct: bool | None = None
    regime_matched: bool | None = None
    thesis_invalidated_by: list[str] = Field(default_factory=list)

    # Outcome classification
    outcome: ReviewOutcome = ReviewOutcome.BREAKEVEN
    cause_class: TradeCause = TradeCause.UNKNOWN

    # Interpretation (explicitly a hypothesis, not a fact)
    interpretation: str = ""
    lessons_proposed: list[str] = Field(default_factory=list)

    created_at: datetime = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()
    )


class Counterfactual(BaseModel):
    """What might have happened under a different decision.

    Clearly labelled as simulation, not fact.
    Must never contaminate actual historical outcomes.
    """

    counterfactual_id: str = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.ids", fromlist=["new_execution_id"]
        ).new_execution_id()
    )
    trade_id: str
    scenario: (
        str  # "entered_later", "didnt_enter", "exited_earlier", "held_longer", "reduced_earlier"
    )
    simulated_pnl: float = 0.0
    simulated_return_pct: float = 0.0
    simulated_mae: float = 0.0
    simulated_mfe: float = 0.0
    assumptions: list[str] = Field(default_factory=list)
    is_simulation: bool = True  # always True — never a fact
    created_at: datetime = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()
    )


class LessonEvidence(BaseModel):
    """Evidence supporting or contradicting a lesson hypothesis."""

    trade_id: str
    supports: bool = True  # True = supports, False = contradicts
    weight: float = 1.0  # how strongly this evidence supports/contradicts
    notes: str = ""


class Lesson(BaseModel):
    """A learnable hypothesis from trade experience.

    Must be validated before influencing system behavior.
    """

    lesson_id: str = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.ids", fromlist=["new_execution_id"]
        ).new_execution_id()
    )
    hypothesis_id: str = ""
    statement: str = ""
    supporting_trade_ids: list[str] = Field(default_factory=list)
    contradicting_trade_ids: list[str] = Field(default_factory=list)
    evidence: list[LessonEvidence] = Field(default_factory=list)
    sample_size: int = 0
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    evidence_quality: str = "insufficient"  # insufficient | weak | moderate | strong
    proposed_change_type: ConfigurationChangeType | None = None
    proposed_change: dict[str, Any] = Field(default_factory=dict)
    status: LessonStatus = LessonStatus.PROPOSED
    created_at: datetime = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()
    )
    validated_at: datetime | None = None
    rejected_at: datetime | None = None
    superseded_by: str | None = None
    configuration_version: str | None = None  # if approved, which version


class DecisionMemoryRecord(BaseModel):
    """Complete decision chain from MarketContext to Outcome.

    This is the canonical record for replay and audit.
    """

    memory_id: str = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.ids", fromlist=["new_execution_id"]
        ).new_execution_id()
    )
    trade_id: str
    thesis_id: str
    context_id: str
    correlation_id: str = ""

    # Complete decision chain
    market_context_snapshot: dict[str, Any] = Field(default_factory=dict)
    agent_opinions: list[dict[str, Any]] = Field(default_factory=list)
    historical_evidence: dict[str, Any] = Field(default_factory=dict)
    synthesis_decision: dict[str, Any] = Field(default_factory=dict)
    risk_decision: dict[str, Any] = Field(default_factory=dict)
    order: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    position_management: list[dict[str, Any]] = Field(default_factory=list)
    outcome: TradeOutcome | None = None
    review: PostTradeReview | None = None
    lessons: list[str] = Field(default_factory=list)  # lesson_ids

    created_at: datetime = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()
    )


class AgentPerformanceRecord(BaseModel):
    """Performance record for a single agent opinion."""

    agent_name: str
    total_opinions: int = 0
    correct_directional: int = 0
    incorrect_directional: int = 0
    caution_calls: int = 0
    no_trade_calls: int = 0
    confidence_sum: float = 0.0
    confidence_correct_sum: float = 0.0
    by_regime: dict[str, dict[str, int]] = Field(default_factory=dict)
    by_instrument: dict[str, dict[str, int]] = Field(default_factory=dict)
    by_timeframe: dict[str, dict[str, int]] = Field(default_factory=dict)
    last_updated: datetime = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()
    )


class ConfigurationVersion(BaseModel):
    """Versioned snapshot of system configuration."""

    version_id: str = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.ids", fromlist=["new_execution_id"]
        ).new_execution_id()
    )
    previous_version: str | None = None
    new_version: str
    change_type: ConfigurationChangeType
    reason: str = ""
    approved_lesson_id: str | None = None
    validation_evidence: list[str] = Field(default_factory=list)
    configuration: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(
        default_factory=lambda: __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()
    )
