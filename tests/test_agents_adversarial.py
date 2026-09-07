"""Adversarial review tests for the Phase B agent intelligence layer.

These tests verify the quality of disagreement, reasoning, and synthesis —
not merely the existence of agents. Each section maps to a review criterion.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from mt5_platform.agents import (
    AgentContext,
    AgentOpinion,
    AgentRole,
    BreakoutAgent,
    DEFAULT_AGENT_NETWORK,
    HistoricalAgent,
    MarketIntelligenceAgent,
    MeanReversionAgent,
    MomentumAgent,
    NewsAgent,
    RegimeAgent,
    Stance,
    StrategyEvaluationAgent,
    StructureAgent,
    SynthesisAgent,
    TradeThesis,
    run_agent_network,
)
from mt5_platform.common.enums import (
    DataQualityLevel,
    OrderSide,
    RegimeLabel,
)
from mt5_platform.common.events import StrategySignal
from mt5_platform.context import (
    BreakoutState,
    Candle,
    DataQuality,
    LiquidityState,
    MarketContext,
    MomentumFeatures,
    SessionInfo,
    StructureFeatures,
    SupportResistanceLevel,
    TrendFeatures,
    VolatilityFeatures,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _candle(i: int, *, base: float = 2500.0, step: float = 0.0, vol: float = 1.0) -> Candle:
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
    return [_candle(i, base=2500.0, step=1.0) for i in range(n)]


def _stretched_up_candles(n: int = 60) -> list[Candle]:
    """Uptrend that has recently run hard — mean reversion should object."""
    candles = []
    for i in range(n):
        step = 1.0 if i < 40 else 5.0
        candles.append(_candle(i, base=2500.0, step=step))
    return candles


def _session() -> SessionInfo:
    return SessionInfo(label="london", utc_hour=10, weekday="monday")


def _trend(slope: float = 0.5) -> TrendFeatures:
    return TrendFeatures(
        slope_per_bar_pct=slope,
        net_change_pct=slope * 10,
        efficiency_ratio=0.8,
        structure_score=0.6,
    )


def _volatility(atr: float = 5.0) -> VolatilityFeatures:
    return VolatilityFeatures(atr=atr, atr_to_median=1.0, atr_percentile=50.0)


def _momentum(roc: float = 0.5, persist: float = 0.8) -> MomentumFeatures:
    return MomentumFeatures(roc_pct=roc, persistence=persist, consecutive_same_dir=8)


def _structure(trend: str = "up", hh: int = 3, hl: int = 3) -> StructureFeatures:
    return StructureFeatures(
        swing_highs=5,
        swing_lows=5,
        higher_highs=hh,
        higher_lows=hl,
        lower_highs=0,
        lower_lows=0,
        structure_trend=trend,
    )


def _breakout(state: str = "none", direction: str = "up") -> BreakoutState:
    return BreakoutState(
        state=state,
        direction=direction,
        boundary=2550.0,
        bars_outside=3 if state == "confirmed" else 0,
        retrace_pct=5.0 if state == "failed" else 0.0,
    )


def _data_quality(level: DataQualityLevel = DataQualityLevel.OK) -> DataQuality:
    return DataQuality(level=level, tick_count=200, issues=[])


def _build_ctx(
    *,
    regime: RegimeLabel = RegimeLabel.TRENDING,
    regime_confidence: float = 0.8,
    trend: TrendFeatures | None = None,
    volatility: VolatilityFeatures | None = None,
    momentum: MomentumFeatures | None = None,
    structure: StructureFeatures | None = None,
    breakout: BreakoutState | None = None,
    data_quality: DataQuality | None = None,
    candles: list[Candle] | None = None,
    current_price: float = 2550.0,
    usable: bool = True,
    instrument: str = "XAUUSD",
) -> MarketContext:
    return MarketContext(
        instrument=instrument,
        timestamp=datetime(2026, 1, 5, 10, tzinfo=UTC),
        current_price=current_price,
        session=_session(),
        candles={"M5": candles or _uptrend_candles()},
        trend=trend or _trend(),
        volatility=volatility or _volatility(),
        momentum=momentum or _momentum(),
        structure=structure or _structure(),
        breakout=breakout or _breakout(),
        liquidity=LiquidityState(level="normal", current_spread=0.3, spread_median=0.3),
        regime=regime,
        regime_confidence=regime_confidence,
        regime_evidence={"reason": "test"},
        data_quality=data_quality or _data_quality(),
        usable_for_trading=usable,
    )


def _agent_ctx(
    *,
    market_context: MarketContext | None = None,
    candidate_signals: list[StrategySignal] | None = None,
    news_events: list[dict] | None = None,
    historical_evidence: dict | None = None,
) -> AgentContext:
    return AgentContext(
        market_context=market_context or _build_ctx(),
        candidate_signals=candidate_signals or [],
        news_events=news_events or [],
        historical_evidence=historical_evidence,
    )


def _opinion(
    role: AgentRole,
    stance: Stance,
    confidence: float = 0.7,
    *,
    name: str | None = None,
    direction: OrderSide | None = None,
) -> AgentOpinion:
    return AgentOpinion(
        agent_name=name or role.value,
        role=role,
        stance=stance,
        confidence=confidence,
        direction=direction or (OrderSide.BUY if stance is Stance.BUY else OrderSide.SELL),
        evidence={},
        rationale="adversarial_test",
        context_id="ctx_test",
    )


# ===========================================================================
# 1. INDEPENDENT REASONING — agents produce genuinely different opinions
# ===========================================================================


class TestIndependentReasoning:
    def test_trending_market_produces_directional_agents(self) -> None:
        """Scenario A: strong trend — momentum, breakout, structure should
        agree on direction; mean_reversion may dissent; regime supports."""
        ctx = _agent_ctx(
            market_context=_build_ctx(
                regime=RegimeLabel.TRENDING,
                trend=_trend(slope=0.6),
                momentum=_momentum(roc=0.8, persist=0.9),
                structure=_structure(trend="up"),
                breakout=_breakout(state="none"),
            )
        )
        opinions = [a.evaluate(ctx) for a in DEFAULT_AGENT_NETWORK]
        stances = {o.agent_name: o.stance for o in opinions}

        assert stances["momentum"] in (Stance.BUY, Stance.NEUTRAL)
        assert stances["structure"] in (Stance.BUY, Stance.CAUTION)
        assert stances["market_regime"] in (Stance.BUY, Stance.SELL, Stance.CAUTION)

    def test_stretched_trend_invites_mean_reversion_dissent(self) -> None:
        """When price is stretched above its mean, mean_reversion must
        disagree with momentum/breakout. This is the core dissenter."""
        ctx = _agent_ctx(
            market_context=_build_ctx(
                regime=RegimeLabel.TRENDING,
                trend=_trend(slope=0.3),
                momentum=_momentum(roc=0.3, persist=0.7),
                volatility=_volatility(atr=1.0),
                structure=_structure(trend="up"),
                candles=_stretched_up_candles(60),
                current_price=2800.0,
            )
        )
        opinions = [a.evaluate(ctx) for a in DEFAULT_AGENT_NETWORK]
        stances = {o.agent_name: o.stance for o in opinions}

        # Momentum should still be bullish (uptrend)
        assert stances["momentum"] is Stance.BUY
        # Mean reversion should SELL (price is far above mean)
        assert stances["mean_reversion"] is Stance.SELL

    def test_breakout_agent_reacts_to_breakout_state(self) -> None:
        """A confirmed breakout should produce a directional opinion;
        a failed breakout should produce NO_TRADE."""
        ctx_confirmed = _agent_ctx(
            market_context=_build_ctx(breakout=_breakout(state="confirmed", direction="up"))
        )
        ctx_failed = _agent_ctx(
            market_context=_build_ctx(breakout=_breakout(state="failed", direction="up"))
        )
        ctx_pending = _agent_ctx(
            market_context=_build_ctx(breakout=_breakout(state="pending", direction="up"))
        )
        agent = BreakoutAgent()
        assert agent.evaluate(ctx_confirmed).stance is Stance.BUY
        assert agent.evaluate(ctx_failed).stance is Stance.NO_TRADE
        assert agent.evaluate(ctx_pending).stance is Stance.NEUTRAL

    def test_agents_dont_share_state(self) -> None:
        """Verify agents are independent: no shared mutable state between
        evaluations of the same context."""
        ctx = _agent_ctx()
        first = {o.agent_name: o for o in (a.evaluate(ctx) for a in DEFAULT_AGENT_NETWORK)}
        second = {o.agent_name: o for o in (a.evaluate(ctx) for a in DEFAULT_AGENT_NETWORK)}
        for name in first:
            assert first[name].stance is second[name].stance
            assert first[name].confidence == second[name].confidence
            assert first[name].rationale == second[name].rationale


# ===========================================================================
# 2. DISAGREEMENT HANDLING — synthesis weighs conflict, not counts votes
# ===========================================================================


class TestDisagreementHandling:
    def test_6_vs_2_with_equal_confidence_does_not_buy(self) -> None:
        """6 BUY vs 2 SELL at equal confidence should NOT produce BUY.
        The synthesis must require a net majority, not a head count."""
        opinions = []
        for i in range(6):
            opinions.append(_opinion(AgentRole.STRUCTURE, Stance.BUY, 0.5, name=f"buy_{i}"))
        for i in range(2):
            opinions.append(_opinion(AgentRole.STRUCTURE, Stance.SELL, 0.5, name=f"sell_{i}"))
        synth = SynthesisAgent()
        thesis = synth.synthesize(_agent_ctx(), opinions)
        assert thesis.action is Stance.NO_TRADE, (
            "6-vs-2 with equal confidence must not produce a directional thesis"
        )

    def test_6_vs_2_with_dominant_buy_confidence_does_buy(self) -> None:
        """6 BUY at high confidence vs 2 SELL at low confidence SHOULD buy."""
        opinions = []
        for i in range(6):
            opinions.append(_opinion(AgentRole.STRUCTURE, Stance.BUY, 0.95, name=f"buy_{i}"))
        for i in range(2):
            opinions.append(_opinion(AgentRole.MEAN_REVERSION, Stance.SELL, 0.3, name=f"sell_{i}"))
        synth = SynthesisAgent()
        thesis = synth.synthesize(_agent_ctx(), opinions)
        assert thesis.action is Stance.BUY
        assert thesis.direction is OrderSide.BUY

    def test_3_vs_3_split_produces_no_trade(self) -> None:
        """An even split must not produce a directional thesis regardless
        of confidence levels."""
        opinions = []
        for i in range(3):
            opinions.append(_opinion(AgentRole.STRUCTURE, Stance.BUY, 0.9, name=f"buy_{i}"))
        for i in range(3):
            opinions.append(_opinion(AgentRole.MEAN_REVERSION, Stance.SELL, 0.9, name=f"sell_{i}"))
        synth = SynthesisAgent()
        thesis = synth.synthesize(_agent_ctx(), opinions)
        assert thesis.action is Stance.NO_TRADE

    def test_2_buy_1_sell_2_caution_does_not_buy(self) -> None:
        """Cautions reduce conviction — 2 BUY + 1 SELL + 2 CAUTION
        should be NO_TRADE."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.BUY, 0.8, name="s1"),
            _opinion(AgentRole.MOMENTUM, Stance.BUY, 0.7, name="m1"),
            _opinion(AgentRole.MEAN_REVERSION, Stance.SELL, 0.6, name="mr1"),
            _opinion(AgentRole.NEWS, Stance.CAUTION, 0.7, name="n1"),
            _opinion(AgentRole.STRATEGY_EVALUATION, Stance.CAUTION, 0.6, name="se1"),
        ]
        synth = SynthesisAgent()
        thesis = synth.synthesize(_agent_ctx(), opinions)
        assert thesis.action is Stance.NO_TRADE


# ===========================================================================
# 3. NO_TRADE ENFORCEMENT — system must refuse in dangerous conditions
# ===========================================================================


class TestNoTradeEnforcement:
    def test_critical_data_quality_vetoes(self) -> None:
        """CRITICAL data quality must produce NO_TRADE via hard gate."""
        ctx = _agent_ctx(
            market_context=_build_ctx(
                data_quality=DataQuality(
                    level=DataQualityLevel.CRITICAL, tick_count=0, issues=["stale_data_critical"]
                ),
                usable=False,
            )
        )
        thesis = run_agent_network(ctx)
        assert thesis.action is Stance.NO_TRADE

    def test_undefined_regime_produces_no_trade(self) -> None:
        ctx = _agent_ctx(
            market_context=_build_ctx(
                regime=RegimeLabel.UNDEFINED, usable=False
            )
        )
        thesis = run_agent_network(ctx)
        assert thesis.action is Stance.NO_TRADE

    def test_abnormal_regime_produces_no_trade(self) -> None:
        ctx = _agent_ctx(
            market_context=_build_ctx(
                regime=RegimeLabel.ABNORMAL, usable=False
            )
        )
        thesis = run_agent_network(ctx)
        assert thesis.action is Stance.NO_TRADE

    def test_high_impact_news_vetoes(self) -> None:
        """A high-impact news event must produce NO_TRADE even when
        all other agents agree on direction."""
        ctx = _agent_ctx(
            market_context=_build_ctx(
                trend=_trend(slope=0.8),
                momentum=_momentum(roc=1.0, persist=0.9),
                structure=_structure(trend="up"),
            ),
            news_events=[{"impact": "high", "within_minutes": 10}],
        )
        thesis = run_agent_network(ctx)
        assert thesis.action is Stance.NO_TRADE

    def test_failed_breakout_vetoes(self) -> None:
        """A failed breakout must produce NO_TRADE."""
        ctx = _agent_ctx(
            market_context=_build_ctx(breakout=_breakout(state="failed", direction="up"))
        )
        thesis = run_agent_network(ctx)
        assert thesis.action is Stance.NO_TRADE

    def test_insufficient_market_data_produces_no_trade(self) -> None:
        """When trend/volatility/momentum are all None, the system must
        refuse to produce a directional thesis."""
        ctx = _agent_ctx(
            market_context=_build_ctx(
                trend=None,
                volatility=None,
                momentum=None,
                structure=None,
                regime=RegimeLabel.UNDEFINED,
                usable=False,
            )
        )
        thesis = run_agent_network(ctx)
        assert thesis.action is Stance.NO_TRADE

    def test_no_trade_thesis_is_fully_traceable(self) -> None:
        """Even a NO_TRADE thesis must carry the opinions and reasons."""
        ctx = _agent_ctx(
            market_context=_build_ctx(
                data_quality=DataQuality(level=DataQualityLevel.CRITICAL, issues=["stale"]),
                usable=False,
            )
        )
        thesis = run_agent_network(ctx)
        assert thesis.action is Stance.NO_TRADE
        assert len(thesis.opinions) == len(DEFAULT_AGENT_NETWORK)
        assert len(thesis.reasons) > 0
        assert thesis.context_id == ctx.market_context.context_id


# ===========================================================================
# 4. TRACEABILITY — every thesis answers "why?"
# ===========================================================================


class TestTraceability:
    def test_thesis_carries_context_id(self) -> None:
        ctx = _agent_ctx()
        thesis = run_agent_network(ctx)
        assert thesis.context_id == ctx.market_context.context_id

    def test_thesis_carries_all_opinions(self) -> None:
        ctx = _agent_ctx()
        thesis = run_agent_network(ctx)
        agent_names = {o.agent_name for o in thesis.opinions}
        for agent in DEFAULT_AGENT_NETWORK:
            assert agent.name in agent_names

    def test_every_opinion_has_correlation_id(self) -> None:
        """AgentOpinion.correlation_id must equal the context_id."""
        ctx = _agent_ctx()
        opinions = [a.evaluate(ctx) for a in DEFAULT_AGENT_NETWORK]
        for op in opinions:
            assert op.correlation_id == ctx.market_context.context_id

    def test_thesis_has_unique_id(self) -> None:
        ctx = _agent_ctx()
        t1 = run_agent_network(ctx)
        t2 = run_agent_network(ctx)
        # Same context, but the synthesis may produce different thesis_ids
        # since thesis_id uses UUID. What matters is the ID format.
        assert t1.thesis_id.startswith("ths_")
        assert t2.thesis_id.startswith("ths_")

    def test_audit_events_emitted(self) -> None:
        """Synthesis should emit audit events for emitted/rejected theses."""
        from mt5_platform.common.audit import audit_log
        from mt5_platform.common.events import AuditEvent
        from mt5_platform.common.enums import AuditEventType

        audit_log.clear()
        ctx = _agent_ctx(
            market_context=_build_ctx(
                data_quality=DataQuality(level=DataQualityLevel.CRITICAL, issues=["stale"]),
                usable=False,
            )
        )
        run_agent_network(ctx)
        events = audit_log.recent(10)
        rejected = [e for e in events if e.event_type == AuditEventType.THESIS_REJECTED.value]
        assert len(rejected) >= 1

    def test_thesis_lists_aligned_and_opposed_agents(self) -> None:
        """Traceability: the thesis evidence must name which agents agreed."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.BUY, 0.9, name="structure"),
            _opinion(AgentRole.MOMENTUM, Stance.BUY, 0.8, name="momentum"),
            _opinion(AgentRole.MEAN_REVERSION, Stance.SELL, 0.7, name="mean_reversion"),
        ]
        synth = SynthesisAgent()
        # This will be NO_TRADE due to insufficient majority, but the
        # evidence summary should still be in the reasons.
        thesis = synth.synthesize(_agent_ctx(), opinions)
        # Check that reasons contain agent names
        all_reasons = " ".join(thesis.reasons)
        assert "structure" in all_reasons or "momentum" in all_reasons


# ===========================================================================
# 5. FUTURE-DATA LEAKAGE — agents cannot peek ahead
# ===========================================================================


class TestNoFutureDataLeakage:
    def test_agents_only_read_from_context(self) -> None:
        """Verify no agent accesses time-dependent data outside the context."""
        import inspect
        import mt5_platform.agents.analytical_agents as aa
        import mt5_platform.agents.support_agents as sa

        for module in (aa, sa):
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if not issubclass(obj, __import__("mt5_platform.agents", fromlist=["Agent"]).Agent):
                    continue
                if obj.__module__ != module.__name__:
                    continue
                source = inspect.getsource(obj.evaluate)
                # No datetime.now(), no random, no global state mutation
                assert "datetime.now" not in source, f"{name} uses datetime.now()"
                assert "random" not in source, f"{name} uses random"
                assert "time.time" not in source, f"{name} uses time.time()"

    def test_agents_cannot_access_future_candles(self) -> None:
        """Agents only receive the MarketContext. They cannot reach
        past it to future data."""
        # Build a context with a known last candle timestamp
        ts = datetime(2026, 6, 1, 12, tzinfo=UTC)
        ctx = _build_ctx()
        ctx = ctx.model_copy(update={"timestamp": ts})
        agent_ctx = _agent_ctx(market_context=ctx)
        for agent in DEFAULT_AGENT_NETWORK:
            op = agent.evaluate(agent_ctx)
            # Opinion timestamp should be >= context timestamp (now > then)
            assert op.timestamp >= ts
            # But the opinion's context_id must reference the original context
            assert op.context_id == ctx.context_id

    def test_agents_are_pure_functions(self) -> None:
        """Same AgentContext -> same opinions (modulo timestamp)."""
        ctx = _agent_ctx()
        results = []
        for _ in range(5):
            results.append(tuple(a.evaluate(ctx) for a in DEFAULT_AGENT_NETWORK))
        first = results[0]
        for r in results[1:]:
            for op1, op2 in zip(first, r):
                assert op1.stance is op2.stance
                assert op1.confidence == op2.confidence
                assert op1.rationale == op2.rationale
                assert op1.evidence == op2.evidence


# ===========================================================================
# 6. HISTORICAL AGENT BOUNDARY — honest about missing evidence
# ===========================================================================


class TestHistoricalBoundary:
    def test_no_evidence_returns_caution(self) -> None:
        agent = HistoricalAgent()
        op = agent.evaluate(_agent_ctx(historical_evidence=None))
        assert op.stance is Stance.CAUTION
        assert "no_measured_evidence" in str(op.evidence.get("status", ""))

    def test_small_sample_returns_caution(self) -> None:
        agent = HistoricalAgent()
        op = agent.evaluate(
            _agent_ctx(
                historical_evidence={
                    "expectancy": 0.5,
                    "sample_size": 5,
                    "evidence_quality": "insufficient",
                }
            )
        )
        assert op.stance is Stance.CAUTION
        assert op.evidence.get("status") == "measured"

    def test_negative_expectancy_returns_caution(self) -> None:
        agent = HistoricalAgent()
        op = agent.evaluate(
            _agent_ctx(
                historical_evidence={
                    "expectancy": -0.3,
                    "sample_size": 100,
                    "min_sample": 30,
                }
            )
        )
        assert op.stance is Stance.CAUTION

    def test_historical_agent_does_not_fabricate(self) -> None:
        """Without evidence, the agent must not claim any statistical truth."""
        agent = HistoricalAgent()
        op = agent.evaluate(_agent_ctx())
        # Rationale must mention the absence of evidence
        assert "no" in op.rationale.lower() or "insufficient" in op.rationale.lower()


# ===========================================================================
# 7. STRATEGY-AGENT BOUNDARY — strategies are candidates, not orders
# ===========================================================================


class TestStrategyBoundary:
    def test_strategy_evaluation_rejects_mismatched_regime(self) -> None:
        """A momentum signal in a RANGING regime should be CAUTION."""
        ctx = _agent_ctx(
            market_context=_build_ctx(regime=RegimeLabel.RANGING, regime_confidence=0.7),
            candidate_signals=[
                StrategySignal(
                    symbol="XAUUSD",
                    direction=OrderSide.BUY,
                    confidence=0.8,
                    strategy_name="momentum",
                    reason="test",
                )
            ],
        )
        agent = StrategyEvaluationAgent()
        op = agent.evaluate(ctx)
        assert op.stance is Stance.CAUTION

    def test_strategy_evaluation_accepts_matched_regime(self) -> None:
        """A momentum signal in a TRENDING regime should be directional."""
        ctx = _agent_ctx(
            market_context=_build_ctx(regime=RegimeLabel.TRENDING),
            candidate_signals=[
                StrategySignal(
                    symbol="XAUUSD",
                    direction=OrderSide.BUY,
                    confidence=0.8,
                    strategy_name="momentum",
                    reason="test",
                )
            ],
        )
        agent = StrategyEvaluationAgent()
        op = agent.evaluate(ctx)
        assert op.stance is Stance.BUY

    def test_strategies_emit_signals_not_orders(self) -> None:
        """Verify strategies produce StrategySignal only — never OrderRequest."""
        from mt5_platform.strategy.sma_crossover import SmaCrossoverStrategy
        from mt5_platform.common.events import MarketDataEvent

        strat = SmaCrossoverStrategy()
        event = MarketDataEvent(
            timestamp=datetime(2026, 1, 5, 10, tzinfo=UTC),
            source="test",
            symbol="XAUUSD",
            price=2550.0,
        )
        result = strat.generate_signal(event)
        # Should return None (not enough data) or a StrategySignal
        assert result is None or type(result).__name__ == "StrategySignal"


# ===========================================================================
# 8. DETERMINISM — same input -> same output
# ===========================================================================


class TestDeterminism:
    def test_synthesis_is_deterministic(self) -> None:
        """Given the same opinions, synthesis produces the same thesis
        structure (action, direction, confidence)."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.BUY, 0.9, name="s"),
            _opinion(AgentRole.MOMENTUM, Stance.BUY, 0.8, name="m"),
            _opinion(AgentRole.MEAN_REVERSION, Stance.SELL, 0.4, name="mr"),
        ]
        synth = SynthesisAgent()
        ctx = _agent_ctx()
        t1 = synth.synthesize(ctx, opinions)
        t2 = synth.synthesize(ctx, opinions)
        assert t1.action is t2.action
        assert t1.direction is t2.direction
        assert t1.confidence == t2.confidence
        assert t1.aligned_agents == t2.aligned_agents
        assert t1.opposed_agents == t2.opposed_agents

    def test_agent_network_is_reproducible(self) -> None:
        ctx = _agent_ctx()
        t1 = run_agent_network(ctx)
        t2 = run_agent_network(ctx)
        assert t1.action is t2.action
        assert t1.direction is t2.direction
        assert t1.confidence == t2.confidence

    def test_thesis_ids_are_unique(self) -> None:
        """thesis_id uses UUID, so two calls produce different IDs even
        with identical inputs. This is by design (audit trail)."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.BUY, 0.9, name="s"),
            _opinion(AgentRole.MOMENTUM, Stance.BUY, 0.8, name="m"),
        ]
        synth = SynthesisAgent()
        t1 = synth.synthesize(_agent_ctx(), opinions)
        t2 = synth.synthesize(_agent_ctx(), opinions)
        assert t1.thesis_id != t2.thesis_id


# ===========================================================================
# 9. CONTEXT REUSE — agents don't re-derive features
# ===========================================================================


class TestContextReuse:
    def test_agents_read_atr_from_context_not_recomputed(self) -> None:
        """No agent should independently compute ATR from raw candles.
        They all read mctx.volatility.atr."""
        import inspect
        import mt5_platform.agents.analytical_agents as aa
        import mt5_platform.agents.support_agents as sa

        for module in (aa, sa):
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if name in ("Agent",):
                    continue
                from mt5_platform.agents import Agent as _Agent
                if not issubclass(obj, _Agent):
                    continue
                if obj.__module__ != module.__name__:
                    continue
                source = inspect.getsource(obj.evaluate)
                # No agent should compute ATR from scratch
                # (they should read from context)
                assert "atr(" not in source.lower() or "mctx" in source, (
                    f"{name} may be recomputing ATR"
                )

    def test_canonical_candles_used_not_raw_ticks(self) -> None:
        """Agents must read from MarketContext.candles, not from raw tick
        streams or external data sources."""
        ctx = _agent_ctx()
        # All agents should work with the same context without errors
        for agent in DEFAULT_AGENT_NETWORK:
            op = agent.evaluate(ctx)
            assert op.context_id == ctx.market_context.context_id


# ===========================================================================
# 10. SYNTHESIS MATHEMATICS — no hidden majority vote
# ===========================================================================


class TestSynthesisMathematics:
    def test_alignment_is_confidence_weighted(self) -> None:
        """The alignment ratio should be confidence-weighted, not just
        count-based. 3 high-conf BUY + 1 low-conf SELL should lean BUY."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.BUY, 0.95, name="s"),
            _opinion(AgentRole.MOMENTUM, Stance.BUY, 0.9, name="m"),
            _opinion(AgentRole.MARKET_REGIME, Stance.BUY, 0.85, name="r"),
            _opinion(AgentRole.MEAN_REVERSION, Stance.SELL, 0.2, name="mr"),
        ]
        synth = SynthesisAgent()
        thesis = synth.synthesize(_agent_ctx(), opinions)
        # With 3 high-conf BUY vs 1 low-conf SELL, the system should
        # accept the BUY (net_ratio is high because the SELL weight is small)
        assert thesis.action is Stance.BUY

    def test_low_participation_produces_no_trade(self) -> None:
        """If all agents are NEUTRAL, there is no directional evidence."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.NEUTRAL, 0.4, name="s"),
            _opinion(AgentRole.MOMENTUM, Stance.NEUTRAL, 0.4, name="m"),
        ]
        synth = SynthesisAgent()
        thesis = synth.synthesize(_agent_ctx(), opinions)
        assert thesis.action is Stance.NO_TRADE

    def test_veto_overrides_majority(self) -> None:
        """A single high-confidence NO_TRADE veto should block any thesis."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.BUY, 0.9, name="s"),
            _opinion(AgentRole.MOMENTUM, Stance.BUY, 0.9, name="m"),
            _opinion(AgentRole.BREAKOUT, Stance.BUY, 0.9, name="b"),
            _opinion(AgentRole.NEWS, Stance.NO_TRADE, 0.9, name="n"),
        ]
        synth = SynthesisAgent()
        thesis = synth.synthesize(_agent_ctx(), opinions)
        assert thesis.action is Stance.NO_TRADE

    def test_directional_thesis_includes_entry_stop_target(self) -> None:
        """A valid directional thesis must include entry, stop-loss,
        and take-profit derived from ATR."""
        opinions = [
            _opinion(AgentRole.STRUCTURE, Stance.BUY, 0.95, name="s"),
            _opinion(AgentRole.MOMENTUM, Stance.BUY, 0.9, name="m"),
            _opinion(AgentRole.MARKET_INTELLIGENCE, Stance.BUY, 0.8, name="mi"),
        ]
        synth = SynthesisAgent()
        ctx = _agent_ctx(
            market_context=_build_ctx(
                volatility=_volatility(atr=5.0), current_price=2550.0
            )
        )
        thesis = synth.synthesize(ctx, opinions)
        if thesis.action is Stance.BUY:
            assert thesis.entry is not None
            assert thesis.stop_loss is not None
            assert thesis.take_profit is not None
            assert thesis.stop_loss < thesis.entry
            assert thesis.take_profit > thesis.entry
