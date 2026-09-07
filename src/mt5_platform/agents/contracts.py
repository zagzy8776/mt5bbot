"""Typed agent contracts (Phase B).

Agents are deterministic analysts over the canonical MarketContext. They
express opinions — never orders. Synthesis weighs their disagreement into a
structured TradeThesis that later passes through the risk engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import StrategySignal, utc_now
from mt5_platform.common.ids import new_thesis_id
from mt5_platform.context import MarketContext


class Stance(StrEnum):
    """An agent's position. Disagreement is expected and must be representable."""

    BUY = "buy"
    SELL = "sell"
    CAUTION = "caution"  # leans against taking a position right now
    NO_TRADE = "no_trade"  # hard objection (veto-capable at high confidence)
    NEUTRAL = "neutral"  # no directional opinion


class AgentRole(StrEnum):
    MARKET_INTELLIGENCE = "market_intelligence"
    MARKET_REGIME = "market_regime"
    STRUCTURE = "structure"
    MOMENTUM = "momentum"
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    HISTORICAL = "historical"
    NEWS = "news"
    STRATEGY_EVALUATION = "strategy_evaluation"
    SYNTHESIS = "synthesis"
    RISK = "risk"
    POSITION_MANAGEMENT = "position_management"
    PERFORMANCE_REVIEW = "performance_review"
    LEARNING = "learning"


class AgentOpinion(BaseModel):
    """One agent's typed, traceable output for one context."""

    agent_name: str
    role: AgentRole
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    direction: OrderSide | None = None  # required for BUY/SELL stances
    evidence: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    context_id: str
    timestamp: datetime = Field(default_factory=utc_now)

    @property
    def correlation_id(self) -> str:
        return self.context_id


class AgentContext(BaseModel):
    """Everything an agent may see. No agent reaches past this object."""

    market_context: MarketContext
    candidate_signals: list[StrategySignal] = Field(default_factory=list)
    news_events: list[dict[str, Any]] = Field(default_factory=list)
    historical_evidence: dict[str, Any] | None = None


class Agent(ABC):
    """Base contract. Deterministic: same AgentContext -> same AgentOpinion."""

    name: str = "agent"
    role: AgentRole = AgentRole.MARKET_INTELLIGENCE
    weight: float = 1.0  # synthesis weight in [0, 1]

    @abstractmethod
    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        raise NotImplementedError

    def _opinion(
        self,
        ctx: AgentContext,
        *,
        stance: Stance,
        confidence: float,
        direction: OrderSide | None = None,
        evidence: dict[str, Any] | None = None,
        rationale: str = "",
    ) -> AgentOpinion:
        if stance in {Stance.BUY, Stance.SELL} and direction is None:
            raise ValueError(f"{self.name}: {stance} opinion requires a direction")
        return AgentOpinion(
            agent_name=self.name,
            role=self.role,
            stance=stance,
            confidence=max(0.0, min(1.0, confidence)),
            direction=direction,
            evidence=evidence or {},
            rationale=rationale,
            context_id=ctx.market_context.context_id,
        )


class TradeThesis(BaseModel):
    """The synthesis layer's structured conclusion. NOT an order.

    Every thesis is fully traceable: it carries the context it was built from
    and every agent opinion that fed it.
    """

    thesis_id: str = Field(default_factory=new_thesis_id)
    context_id: str
    instrument: str
    created_at: datetime = Field(default_factory=utc_now)
    # BUY/SELL thesis, or NO_TRADE with direction None
    action: Stance
    direction: OrderSide | None = None
    regime: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    aligned_agents: int = 0
    opposed_agents: int = 0
    caution_agents: int = 0
    neutral_agents: int = 0
    total_agents: int = 0
    historical_expectancy: str | None = None
    volatility_state: str | None = None
    news_risk: str | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    invalidation: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    opinions: list[AgentOpinion] = Field(default_factory=list)