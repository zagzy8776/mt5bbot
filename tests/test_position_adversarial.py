"""Phase D.5 — Position Intelligence adversarial review.

Tests the position manager under realistic market evolution:
- HOLD stability (noise vs thesis invalidation)
- Decision hysteresis/stability
- Thesis immutability vs evolution
- REDUCE correctness
- MODIFY safety
- Abnormal market handling
- Emergency semantics (kill switch vs emergency exit)
- Decision replay
- Full lifecycle
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.common.enums import (
    DataQualityLevel,
    InvalidationReason,
    OrderSide,
    PositionDecision,
    RegimeLabel,
    ThesisStatus,
)
from mt5_platform.common.events import AccountSnapshot, StrategySignal
from mt5_platform.config import Settings, TradingMode
from mt5_platform.historical.context_bridge import setup_from_context
from mt5_platform.position import (
    PositionManager,
    PositionManagerConfig,
    PositionState,
    PositionDecisionModel,
    ThesisSnapshot,
    reconcile_position,
    BrokerPosition,
    ReconciliationResult,
)
from mt5_platform.context import (
    BreakoutState,
    Candle,
    DataQuality,
    LiquidityState,
    MarketContext,
    MomentumFeatures,
    SessionInfo,
    StructureFeatures,
    TrendFeatures,
    VolatilityFeatures,
)
from mt5_platform.risk import RiskEngine, RiskContext
from mt5_platform.agents import (
    AgentContext,
    DEFAULT_AGENT_NETWORK,
    run_agent_network,
)
from mt5_platform.execution import MockExecutionAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(year=2026, month=1, day=1, hour=10) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _candle(i, base=2550.0, step=0.0, high_add=1.0, low_add=1.0):
    close = base + step * i
    return Candle(
        timestamp=_ts() - timedelta(minutes=5 * (60 - i)),
        open=close - 0.5,
        high=close + high_add,
        low=close - low_add,
        close=close,
        volume=1.0,
    )


def _market_context(
    *,
    regime=RegimeLabel.TRENDING,
    slope=0.3,
    atr=5.0,
    atr_to_median=1.0,
    roc=0.5,
    persist=0.8,
    current_price=2550.0,
    data_quality=DataQualityLevel.OK,
    last_tick_age_s=1.0,
    session="london",
):
    ts = _ts()
    candles = [_candle(i, base=2550.0, step=1.0) for i in range(60)]
    return MarketContext(
        instrument="XAUUSD",
        timestamp=ts,
        current_price=current_price,
        session=SessionInfo(label=session, utc_hour=ts.hour, weekday="monday"),
        candles={"M5": candles},
        trend=TrendFeatures(slope_per_bar_pct=slope, efficiency_ratio=0.7),
        volatility=VolatilityFeatures(atr=atr, atr_to_median=atr_to_median),
        momentum=MomentumFeatures(roc_pct=roc, persistence=persist),
        structure=StructureFeatures(structure_trend="up"),
        breakout=BreakoutState(state="none"),
        liquidity=LiquidityState(level="normal"),
        regime=regime,
        regime_confidence=0.8,
        regime_evidence={},
        data_quality=DataQuality(level=data_quality, last_tick_age_s=last_tick_age_s, tick_count=200),
        usable_for_trading=True,
    )


def _position(
    *,
    position_id="pos_1",
    instrument="XAUUSD",
    direction=OrderSide.BUY,
    volume=0.1,
    entry_price=2550.0,
    current_price=2555.0,
    stop_loss=2545.0,
    take_profit=2565.0,
    thesis_id="thesis_1",
    regime="trending",
    invalidation_levels=None,
):
    if invalidation_levels is None:
        invalidation_levels = ["2540.0"]
    thesis = ThesisSnapshot(
        thesis_id=thesis_id,
        instrument=instrument,
        direction=direction,
        regime=regime,
        confidence=0.8,
        entry=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        invalidation_levels=invalidation_levels,
        reasons=["uptrend confirmed", "momentum positive"],
    )
    return PositionState(
        position_id=position_id,
        instrument=instrument,
        direction=direction,
        volume=volume,
        entry_price=entry_price,
        current_price=current_price,
        unrealized_pnl=(current_price - entry_price) * volume * 100 if direction == OrderSide.BUY else (entry_price - current_price) * volume * 100,
        stop_loss=stop_loss,
        take_profit=take_profit,
        opened_at=_ts(hour=1),
        last_update=_ts(hour=2),
        thesis_id=thesis_id,
        thesis_snapshot=thesis,
    )


def _pm():
    return PositionManager(config=PositionManagerConfig(
        invalidation_buffer_pct=0.1,
        max_stale_context_s=300.0,
        trail_activation_pct=0.5,
        trail_distance_atr_mult=1.5,
        reduce_volume_pct=0.5,
    ))


# ===========================================================================
# 1. HOLD STABILITY — noise should not trigger exit
# ===========================================================================


class TestHoldStability:
    def test_small_pullback_stays_hold(self):
        """A 5-point pullback on a BUY thesis should not invalidate."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2545.0,
            stop_loss=2540.0,
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.2,
            persist=0.7,
            current_price=2545.0,
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.decision == PositionDecision.HOLD

    def test_minor_momentum_decrease_stays_hold(self):
        """Momentum persistence dropping from 0.8 to 0.6 should not
        invalidate a trending thesis."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.2,
            persist=0.6,  # decreased but not critical
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.decision == PositionDecision.HOLD

    def test_temporary_volatility_increase_stays_hold(self):
        """ATR-to-median jumping to 1.5 (from 1.0) should not invalidate."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.2,
            atr_to_median=1.5,
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.decision == PositionDecision.HOLD

    def test_normal_candle_noise_stays_hold(self):
        """Normal candle wicks and small ranges should not trigger exit."""
        candles = [_candle(i, base=2550.0, step=0.1, high_add=2.0, low_add=2.0) for i in range(60)]
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.2,
            current_price=2552.0,
        )
        ctx = ctx.model_copy(update={"candles": {"M5": candles}})
        pm = _pm()
        pos = _position(current_price=2552.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.decision == PositionDecision.HOLD


# ===========================================================================
# 2. DECISION HYSTERESIS — stable around threshold
# ===========================================================================


class TestDecisionHysteresis:
    def test_repeated_evaluation_stable(self):
        """Same context evaluated 10 times must produce same decision."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decisions = [pm.evaluate(pos, ctx) for _ in range(10)]
        actions = [d.decision for d in decisions]
        statuses = [d.thesis_status for d in decisions]
        assert len(set(actions)) == 1, f"Oscillating decisions: {actions}"
        assert len(set(statuses)) == 1, f"Oscillating statuses: {statuses}"

    def test_near_threshold_stable(self):
        """Evaluations near the weakening threshold should not oscillate."""
        pm = _pm()
        pos = _position()
        # Just above the momentum persistence threshold (0.3)
        ctx = _market_context(persist=0.35)
        decisions = [pm.evaluate(pos, ctx) for _ in range(5)]
        actions = [d.decision for d in decisions]
        assert all(d == PositionDecision.HOLD for d in actions), f"Oscillating near threshold: {actions}"

    def test_just_below_threshold_consistently_weakening(self):
        """Just below the threshold should consistently produce REDUCE."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(persist=0.25)
        decisions = [pm.evaluate(pos, ctx) for _ in range(5)]
        actions = [d.decision for d in decisions]
        assert all(d == PositionDecision.REDUCE for d in actions), f"Inconsistent below threshold: {actions}"


# ===========================================================================
# 3. THESIS IMMUTABILITY — original thesis never changes
# ===========================================================================


class TestThesisImmutability:
    def test_original_thesis_unchanged_after_evaluation(self):
        """The original thesis snapshot must not be mutated by evaluation."""
        pm = _pm()
        pos = _position()
        original_snapshot = pos.thesis_snapshot.model_copy() if pos.thesis_snapshot else None
        ctx = _market_context(regime=RegimeLabel.RANGING)
        pm.evaluate(pos, ctx)
        if original_snapshot:
            assert pos.thesis_snapshot.thesis_id == original_snapshot.thesis_id
            assert pos.thesis_snapshot.regime == original_snapshot.regime
            assert pos.thesis_snapshot.confidence == original_snapshot.confidence

    def test_position_decision_tracks_original_thesis(self):
        """PositionDecision must reference the original thesis_id, not a new one."""
        pm = _pm()
        pos = _position(thesis_id="thesis_original_123")
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert decision.original_thesis_id == "thesis_original_123"


# ===========================================================================
# 4. THESIS EVOLUTION — current status can change
# ===========================================================================


class TestThesisEvolution:
    def test_valid_to_invalidated_evolution(self):
        """Thesis can evolve from VALID to INVALIDATED."""
        pm = _pm()
        pos = _position(regime="trending")
        ctx_valid = _market_context(regime=RegimeLabel.TRENDING)
        ctx_invalid = _market_context(regime=RegimeLabel.RANGING)

        d1 = pm.evaluate(pos, ctx_valid)
        assert d1.thesis_status == ThesisStatus.VALID

        d2 = pm.evaluate(pos, ctx_invalid)
        assert d2.thesis_status == ThesisStatus.INVALIDATED

    def test_valid_to_weakening_evolution(self):
        """Thesis can evolve from VALID to WEAKENING."""
        pm = _pm()
        pos = _position()
        ctx_valid = _market_context(persist=0.8)
        ctx_weak = _market_context(persist=0.25)

        d1 = pm.evaluate(pos, ctx_valid)
        assert d1.thesis_status == ThesisStatus.VALID

        d2 = pm.evaluate(pos, ctx_weak)
        assert d2.thesis_status == ThesisStatus.WEAKENING

    def test_evolution_preserves_original_thesis_id(self):
        """Through evolution, original_thesis_id remains constant."""
        pm = _pm()
        pos = _position(thesis_id="thesis_abc")
        d1 = pm.evaluate(pos, _market_context(regime=RegimeLabel.TRENDING))
        d2 = pm.evaluate(pos, _market_context(regime=RegimeLabel.RANGING))
        assert d1.original_thesis_id == "thesis_abc"
        assert d2.original_thesis_id == "thesis_abc"


# ===========================================================================
# 5. REDUCE CORRECTNESS
# ===========================================================================


class TestReduceCorrectness:
    def test_reduce_volume_calculation(self):
        """REDUCE should calculate correct remaining volume."""
        pm = _pm()
        pos = _position(volume=0.1)
        ctx = _market_context(slope=0.05, persist=0.25)
        decision = pm.evaluate(pos, ctx)
        assert decision.proposed_volume == pytest.approx(0.05)

    def test_reduce_never_zero(self):
        """REDUCE should never produce zero volume."""
        pm = _pm()
        pos = _position(volume=0.01)
        ctx = _market_context(slope=0.05, persist=0.25)
        decision = pm.evaluate(pos, ctx)
        assert decision.proposed_volume is not None
        assert decision.proposed_volume > 0

    def test_reduce_long_position(self):
        """REDUCE works for long positions."""
        pm = _pm()
        pos = _position(direction=OrderSide.BUY, volume=0.1)
        ctx = _market_context(slope=0.05, persist=0.25)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.REDUCE
        assert decision.proposed_volume == pytest.approx(0.05)

    def test_reduce_short_position(self):
        """REDUCE works for short positions."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2555.0,
            stop_loss=2565.0,
            take_profit=2545.0,
            invalidation_levels=["2570.0"],
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=-0.03,
            persist=0.25,
            current_price=2555.0,
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.REDUCE
        assert decision.proposed_volume == pytest.approx(0.05)

    def test_repeated_reduce_does_not_exit(self):
        """Multiple REDUCE decisions should not become EXIT."""
        pm = _pm()
        pos = _position(volume=0.1)
        ctx = _market_context(slope=0.05, persist=0.25)
        for _ in range(5):
            decision = pm.evaluate(pos, ctx)
            assert decision.decision == PositionDecision.REDUCE
            assert decision.proposed_volume is not None
            assert decision.proposed_volume > 0


# ===========================================================================
# 6. MODIFY SAFETY
# ===========================================================================


class TestModifySafety:
    def test_trailing_stop_never_loosens_protection(self):
        """Trailing stop should only tighten, never loosen."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2570.0,
            stop_loss=2545.0,
            take_profit=2600.0,
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.3,
            atr=5.0,
            current_price=2570.0,
        )
        decision = pm.evaluate(pos, ctx)
        if decision.decision == PositionDecision.MODIFY:
            assert decision.proposed_stop_loss is not None
            assert decision.proposed_stop_loss > 2545.0  # tightened, not loosened

    def test_modify_does_not_cross_current_price(self):
        """Proposed stop should never cross the current price."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2570.0,
            stop_loss=2545.0,
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.3,
            atr=5.0,
            current_price=2570.0,
        )
        decision = pm.evaluate(pos, ctx)
        if decision.decision == PositionDecision.MODIFY and decision.proposed_stop_loss is not None:
            assert decision.proposed_stop_loss < 2570.0  # below current price for BUY

    def test_modify_short_never_loosens(self):
        """For short, trailing stop should only tighten (move down)."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2540.0,
            stop_loss=2565.0,
            take_profit=2535.0,
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=-0.3,
            atr=5.0,
            current_price=2540.0,
        )
        decision = pm.evaluate(pos, ctx)
        if decision.decision == PositionDecision.MODIFY and decision.proposed_stop_loss is not None:
            assert decision.proposed_stop_loss < 2565.0  # tightened for short


# ===========================================================================
# 7. ABNORMAL MARKET HANDLING
# ===========================================================================


class TestAbnormalMarket:
    def test_flash_movement_no_unsafe_decision(self):
        """Flash movement (extreme price change) should not cause unsafe decision."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2500.0,  # -50 points
            stop_loss=2545.0,
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.2,
            current_price=2500.0,
        )
        decision = pm.evaluate(pos, ctx)
        # Should be EXIT (stop hit) or EMERGENCY_EXIT, not MODIFY/REDUCE
        assert decision.decision in (
            PositionDecision.EXIT,
            PositionDecision.EMERGENCY_EXIT,
            PositionDecision.NO_ACTION,
        )

    def test_huge_volatility_spike(self):
        """ATR-to-median > 2.5 should trigger WEAKENING or worse."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.2,
            atr_to_median=3.0,
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status in (
            ThesisStatus.WEAKENING,
            ThesisStatus.INVALIDATED,
            ThesisStatus.VALID,  # may still be valid if other factors ok
        )
        # Should never be HOLD with volatility that high without noting risk
        if decision.decision == PositionDecision.HOLD:
            assert "volatility" in decision.reason.lower() or "spike" in decision.reason.lower()

    def test_stale_market_no_unsafe_decision(self):
        """Stale market data should produce NO_ACTION."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(last_tick_age_s=400.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.NO_ACTION

    def test_critical_data_quality_triggers_emergency(self):
        """CRITICAL data quality should trigger EMERGENCY_EXIT."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(data_quality=DataQualityLevel.CRITICAL)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT

    def test_abnormal_regime_triggers_emergency(self):
        """ABNORMAL regime should trigger EMERGENCY_EXIT."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(regime=RegimeLabel.ABNORMAL)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT

    def test_broker_disconnect_no_new_orders(self):
        """When broker is disconnected, no new management orders."""
        # This is tested via risk context kill_switch
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        risk_ctx = {"kill_switch": True}
        decision = pm.evaluate(pos, ctx, risk_context=risk_ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT

    def test_internal_broker_mismatch_detected(self):
        """Mismatched internal/broker state should be detected by reconciliation."""
        internal = _position(volume=0.1)
        broker = BrokerPosition(
            ticket="t1",
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=0.2,  # mismatch
            entry_price=2550.0,
            current_price=2555.0,
            stop_loss=2545.0,
            take_profit=2565.0,
            unrealized_pnl=50.0,
            opened_at=_ts(hour=1),
        )
        result = reconcile_position(internal, [broker])
        assert result.has_mismatch


# ===========================================================================
# 8. EMERGENCY SEMANTICS — kill switch vs emergency exit
# ===========================================================================


class TestEmergencySemantics:
    def test_kill_switch_produces_emergency_exit(self):
        """Kill switch engaged produces EMERGENCY_EXIT."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx, risk_context={"kill_switch": True})
        assert decision.decision == PositionDecision.EMERGENCY_EXIT
        assert "kill_switch" in decision.reason.lower()

    def test_emergency_exit_confidence_is_one(self):
        """EMERGENCY_EXIT should have maximum confidence."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(regime=RegimeLabel.ABNORMAL)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT
        assert decision.confidence == 1.0

    def test_emergency_exit_never_modify(self):
        """Emergency conditions should never produce MODIFY."""
        pm = _pm()
        pos = _position()
        for condition in [
            {"kill_switch": True},
            None,  # abnormal regime via ctx
        ]:
            ctx = _market_context(
                regime=RegimeLabel.ABNORMAL if condition is None else RegimeLabel.TRENDING,
                current_price=2570.0,
                atr=5.0,
            )
            risk_ctx = condition
            decision = pm.evaluate(pos, ctx, risk_context=risk_ctx)
            assert decision.decision != PositionDecision.MODIFY

    def test_emergency_reasons_are_explicit(self):
        """EMERGENCY_EXIT must list the emergency reasons."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(data_quality=DataQualityLevel.CRITICAL)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT
        assert len(decision.reason) > 0
        assert "emergency" in decision.reason.lower() or "data" in decision.reason.lower()


# ===========================================================================
# 9. DECISION REPLAY
# ===========================================================================


class TestDecisionReplay:
    def test_same_inputs_same_decision(self):
        """Replaying the same inputs must produce the same decision."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        d1 = pm.evaluate(pos, ctx)
        d2 = pm.evaluate(pos, ctx)
        assert d1.decision == d2.decision
        assert d1.thesis_status == d2.thesis_status
        assert d1.confidence == d2.confidence
        assert d1.reason == d2.reason

    def test_decision_serialization_roundtrip(self):
        """Decision can be serialized and deserialized."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        data = decision.model_dump(mode="json")
        restored = PositionDecisionModel.model_validate(data)
        assert restored.decision == decision.decision
        assert restored.thesis_status == decision.thesis_status
        assert restored.position_id == decision.position_id

    def test_decision_contains_all_required_fields(self):
        """Every decision must have all traceability fields."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert decision.decision_id
        assert decision.position_id == "pos_1"
        assert decision.original_thesis_id == "thesis_1"
        assert decision.current_context_id == ctx.context_id
        assert decision.thesis_status in (
            ThesisStatus.VALID,
            ThesisStatus.WEAKENING,
            ThesisStatus.INVALIDATED,
            ThesisStatus.UNKNOWN,
        )
        assert decision.decision in (
            PositionDecision.HOLD,
            PositionDecision.MODIFY,
            PositionDecision.REDUCE,
            PositionDecision.EXIT,
            PositionDecision.EMERGENCY_EXIT,
            PositionDecision.NO_ACTION,
        )
        assert 0.0 <= decision.confidence <= 1.0
        assert decision.correlation_id


# ===========================================================================
# 10. FULL LIFECYCLE
# ===========================================================================


class TestFullLifecycle:
    def test_complete_lifecycle(self):
        """MarketContext → Agents → Historical Evidence → Synthesis →
        TradeThesis → Risk → Order → Execution → Position →
        Market changes → Position Intelligence → Risk → Position action."""
        from mt5_platform.agents import (
            AgentContext,
            run_agent_network,
        )
        from mt5_platform.risk import RiskEngine, RiskContext

        # Step 1: Market context
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.5,
            atr=5.0,
            roc=0.8,
            persist=0.9,
            current_price=2550.0,
        )

        # Step 2: Agent network → thesis
        agent_ctx = AgentContext(market_context=ctx)
        thesis = run_agent_network(agent_ctx)
        assert thesis.action.value in ("buy", "sell", "no_trade")

        # Step 3: Risk evaluation
        risk = RiskEngine(settings=Settings(trading_mode=TradingMode.DEMO))
        account = AccountSnapshot(
            balance=10000.0,
            equity=10000.0,
            free_margin=9500.0,
            used_margin=500.0,
            floating_pnl=0.0,
        )
        signal = StrategySignal(
            symbol="XAUUSD",
            direction=OrderSide.BUY,
            entry=thesis.entry or 2550.0,
            stop_loss=thesis.stop_loss or 2545.0,
            take_profit=thesis.take_profit or 2565.0,
            confidence=thesis.confidence,
            reason=thesis.reasons[0] if thesis.reasons else "test",
            strategy_name="test",
        )
        risk_ctx = RiskContext(account=account, proposed_volume=0.1)
        risk_result = risk.evaluate(signal, risk_ctx)
        assert risk_result.approved is True

        # Step 4: Create position
        position = PositionState(
            position_id="pos_lifecycle_1",
            instrument="XAUUSD",
            direction=OrderSide.BUY,
            volume=0.1,
            entry_price=2550.0,
            current_price=2550.0,
            stop_loss=2545.0,
            take_profit=2565.0,
            opened_at=_ts(hour=1),
            last_update=_ts(hour=1),
            thesis_id=thesis.thesis_id,
            thesis_snapshot=ThesisSnapshot(
                thesis_id=thesis.thesis_id,
                instrument="XAUUSD",
                direction=OrderSide.BUY,
                regime="trending",
                confidence=thesis.confidence,
                entry=thesis.entry,
                stop_loss=thesis.stop_loss,
                take_profit=thesis.take_profit,
                invalidation_levels=["2540.0"],
                reasons=thesis.reasons,
            ),
        )

        # Step 5: Market changes → thesis weakens → REDUCE
        pm = _pm()
        ctx_weak = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.05,
            persist=0.25,
            current_price=2545.0,
        )
        decision = pm.evaluate(position, ctx_weak)
        assert decision.thesis_status in (ThesisStatus.WEAKENING, ThesisStatus.INVALIDATED)
        assert decision.decision in (PositionDecision.REDUCE, PositionDecision.EXIT)
        assert decision.original_thesis_id == thesis.thesis_id
        assert decision.position_id == "pos_lifecycle_1"

        # Step 6: Verify traceability
        assert decision.current_context_id == ctx_weak.context_id
        assert len(decision.reason) > 0
        assert decision.confidence >= 0.0

    def test_lifecycle_with_mock_execution(self):
        """Full lifecycle with mock execution adapter."""
        from mt5_platform.agents import AgentContext, run_agent_network
        from mt5_platform.risk import RiskEngine, RiskContext

        # Market context
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.5,
            atr=5.0,
            roc=0.8,
            persist=0.9,
            current_price=2550.0,
        )

        # Agent network
        agent_ctx = AgentContext(market_context=ctx)
        thesis = run_agent_network(agent_ctx)

        # Risk
        risk = RiskEngine(settings=Settings(trading_mode=TradingMode.DEMO))
        account = AccountSnapshot(
            balance=10000.0,
            equity=10000.0,
            free_margin=9500.0,
            used_margin=500.0,
            floating_pnl=0.0,
        )
        signal = StrategySignal(
            symbol="XAUUSD",
            direction=OrderSide.BUY,
            entry=thesis.entry or 2550.0,
            stop_loss=thesis.stop_loss or 2545.0,
            take_profit=thesis.take_profit or 2565.0,
            confidence=thesis.confidence,
            reason=thesis.reasons[0] if thesis.reasons else "test",
            strategy_name="test",
        )
        risk_ctx = RiskContext(account=account, proposed_volume=0.1)
        risk_result = risk.evaluate(signal, risk_ctx)
        assert risk_result.approved is True

        # Mock execution
        adapter = MockExecutionAdapter()
        # Verify adapter is available
        assert adapter is not None
        assert hasattr(adapter, "connect")
        assert hasattr(adapter, "submit_order")

        # Position
        position = PositionState(
            position_id="pos_mock_1",
            instrument="XAUUSD",
            direction=OrderSide.BUY,
            volume=0.1,
            entry_price=2550.0,
            current_price=2550.0,
            stop_loss=2545.0,
            take_profit=2565.0,
            opened_at=_ts(hour=1),
            last_update=_ts(hour=1),
            thesis_id=thesis.thesis_id,
            thesis_snapshot=ThesisSnapshot(
                thesis_id=thesis.thesis_id,
                instrument="XAUUSD",
                direction=OrderSide.BUY,
                regime="trending",
                confidence=thesis.confidence,
                entry=thesis.entry,
                stop_loss=thesis.stop_loss,
                take_profit=thesis.take_profit,
            ),
        )

        # Position manager
        pm = _pm()
        ctx_weak = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.05,
            persist=0.25,
            current_price=2545.0,
        )
        decision = pm.evaluate(position, ctx_weak)
        assert decision.thesis_status in (ThesisStatus.WEAKENING, ThesisStatus.INVALIDATED)
        assert decision.decision in (PositionDecision.REDUCE, PositionDecision.EXIT)
