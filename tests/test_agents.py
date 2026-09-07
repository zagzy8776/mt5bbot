"""Tests for the multi-agent intelligence layer (Phase B).

Deterministic agents analyze the canonical MarketContext and express typed
opinions. The SynthesisAgent weighs their disagreement into a structured
TradeThesis. Every conclusion must be traceable to measurable features.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from mt5_platform.agents import (
    AgentContext,
    AgentRole,
    MarketIntelligenceAgent,
    Stance,
)
from mt5_platform.common.enums import (
    DataQualityLevel,
    OrderSide,
    RegimeLabel,
)
from mt5_platform.common.events import StrategySignal
from mt5_platform.context import (
    Candle,
    DataQuality,
    MarketContext,
    SessionInfo,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(i: int, *, step: float = 0.0, base: float = 2500.0, vol: float = 1.0) -> Candle:
    close = base + step * i
    return Candle(
        timestamp=datetime(2026, 1, 5, 10, tzinfo=UTC),
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=vol,
    )


def _uptrend_candles(n: int = 60) -> list[Candle]:
    return [_candle(i, step=1.0) for i in range(n)]


def _range_candles(n: int = 60, *, width: float = 5.0) -> list[Candle]:
    return [_candle(i, step=math.sin(i / 3.0) * width / 2.0) for i in range(n)]


def _session() -> SessionInfo:
    return SessionInfo(label="london", utc_hour=10, weekday="monday")


def _market_context(
    *,
    candles: list[Candle] | None = None,
    regime: RegimeLabel | None = RegimeLabel.TRENDING,
    regime_confidence: float = 0.8,
    data_quality: DataQuality | None = None,
    current_price: float = 2550.0,
    usable: bool = True,
) -> MarketContext:
    return MarketContext(
        instrument="XAUUSD",
        timestamp=datetime(2026, 1, 5, 10, tzinfo=UTC),
        current_price=current_price,
        session=_session(),
        candles={"M5": candles or _uptrend_candles()},
        regime=regime,
        regime_confidence=regime_confidence,
        data_quality=data_quality or DataQuality(level=DataQualityLevel.OK),
        usable_for_trading=usable,
    )


def _agent_context(
    *,
    market_context: MarketContext | None = None,
    candidate_signals: list[StrategySignal] | None = None,
    news_events: list[dict] | None = None,
    historical_evidence: dict | None = None,
) -> AgentContext:
    return AgentContext(
        market_context=market_context or _market_context(),
        candidate_signals=candidate_signals or [],
        news_events=news_events or [],
        historical_evidence=historical_evidence,
    )


def _buy_signal() -> StrategySignal:
    return StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2550.0,
        stop_loss=2540.0,
        take_profit=2570.0,
        confidence=0.7,
        strategy_name="sma_crossover",
        reason="test signal",
    )


# ---------------------------------------------------------------------------
# Agent contracts
# ---------------------------------------------------------------------------


def test_stance_enum_values() -> None:
    assert Stance.BUY == "buy"
    assert Stance.SELL == "sell"
    assert Stance.CAUTION == "caution"
    assert Stance.NO_TRADE == "no_trade"
    assert Stance.NEUTRAL == "neutral"


def test_agent_role_coverage() -> None:
    roles = {r for r in AgentRole}
    for expected in [
        "market_intelligence",
        "market_regime",
        "structure",
        "momentum",
        "breakout",
        "mean_reversion",
        "historical",
        "news",
        "strategy_evaluation",
        "synthesis",
        "risk",
        "position_management",
        "performance_review",
        "learning",
    ]:
        assert expected in roles


def test_agent_opinion_requires_direction_for_buy() -> None:
    agent = MarketIntelligenceAgent()
    ctx = _agent_context()
    with pytest.raises(ValueError, match="requires a direction"):
        agent._opinion(ctx, stance=Stance.BUY, confidence=0.5)


def test_agent_opinion_confidence_clamped() -> None:
    agent = MarketIntelligenceAgent()
    ctx = _agent_context()
    op = agent._opinion(ctx, stance=Stance.NEUTRAL, confidence=1.5)
    assert op.confidence == 1.0
    op2 = agent._opinion(ctx, stance=Stance.NEUTRAL, confidence=-0.5)
    assert op2.confidence == 0.0


def test_agent_correlation_id_is_context_id() -> None:
    agent = MarketIntelligenceAgent()
    ctx = _agent_context()
    op = agent._opinion(ctx, stance=Stance.NEUTRAL, confidence=0.3)
    assert op.correlation_id == ctx.market_context.context_id
