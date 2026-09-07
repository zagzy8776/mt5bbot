"""Phase D — Position Intelligence adversarial tests.

Covers:
- PositionDecision contract completeness
- Thesis tracking (original vs current)
- HOLD with explicit reasoning
- MODIFY (trailing stop, within constraints)
- REDUCE (thesis weakening)
- EXIT (thesis invalidated)
- EMERGENCY_EXIT (kill switch, data critical, abnormal regime)
- Risk integration boundary
- Broker reconciliation boundary
- Traceability (position_id, thesis_id, context_id, correlation_id)
- Determinism
- Long/short position correctness
- Stale context -> NO_ACTION
- Missing context -> NO_ACTION
- Full integration lifecycle
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.common.enums import (
    DataQualityLevel,
    OrderSide,
    PositionDecision,
    RegimeLabel,
    ThesisStatus,
)
from mt5_platform.common.events import AccountSnapshot, StrategySignal
from mt5_platform.config import Settings, TradingMode
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
from mt5_platform.position import (
    BrokerPosition,
    PositionDecisionModel,
    PositionManager,
    PositionManagerConfig,
    PositionState,
    ThesisSnapshot,
    reconcile_position,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(year=2026, month=1, day=1, hour=10) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _candle(i, base=2550.0, step=0.0):
    close = base + step * i
    return Candle(
        timestamp=_ts() - timedelta(minutes=5 * (60 - i)),
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1.0,
    )


def _market_context(
    *,
    regime=RegimeLabel.TRENDING,
    slope=0.3,
    atr=5.0,
    roc=0.5,
    persist=0.8,
    current_price=2550.0,
    data_quality=DataQualityLevel.OK,
    last_tick_age_s=1.0,
):
    ts = _ts()
    candles = [_candle(i, base=2550.0, step=1.0) for i in range(60)]
    return MarketContext(
        instrument="XAUUSD",
        timestamp=ts,
        current_price=current_price,
        session=SessionInfo(label="london", utc_hour=ts.hour, weekday="monday"),
        candles={"M5": candles},
        trend=TrendFeatures(slope_per_bar_pct=slope, efficiency_ratio=0.7),
        volatility=VolatilityFeatures(atr=atr, atr_to_median=1.0),
        momentum=MomentumFeatures(roc_pct=roc, persistence=persist),
        structure=StructureFeatures(structure_trend="up"),
        breakout=BreakoutState(state="none"),
        liquidity=LiquidityState(level="normal"),
        regime=regime,
        regime_confidence=0.8,
        regime_evidence={},
        data_quality=DataQuality(
            level=data_quality,
            last_tick_age_s=last_tick_age_s,
            tick_count=200,
        ),
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
        unrealized_pnl=(
            (current_price - entry_price) * volume * 100
            if direction == OrderSide.BUY
            else (entry_price - current_price) * volume * 100
        ),
        stop_loss=stop_loss,
        take_profit=take_profit,
        opened_at=_ts(hour=1),
        last_update=_ts(hour=2),
        thesis_id=thesis_id,
        thesis_snapshot=thesis,
    )


def _pm():
    return PositionManager(
        config=PositionManagerConfig(
            invalidation_buffer_pct=0.1,
            max_stale_context_s=300.0,
            trail_activation_pct=0.5,
            trail_distance_atr_mult=1.5,
            reduce_volume_pct=0.5,
        )
    )


# ===========================================================================
# 1. CONTRACT — PositionDecision has all required fields
# ===========================================================================


class TestPositionContract:
    def test_decision_has_position_id(self):
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert decision.position_id == "pos_1"

    def test_decision_has_thesis_ids(self):
        pm = _pm()
        pos = _position(thesis_id="thesis_abc")
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert decision.original_thesis_id == "thesis_abc"
        assert decision.current_context_id == ctx.context_id

    def test_decision_has_correlation_id(self):
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        d1 = pm.evaluate(pos, ctx)
        d2 = pm.evaluate(pos, ctx)
        assert d1.correlation_id != d2.correlation_id  # unique per evaluation

    def test_decision_has_timestamp(self):
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert decision.timestamp is not None

    def test_decision_has_confidence(self):
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert 0.0 <= decision.confidence <= 1.0

    def test_decision_has_reason(self):
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert len(decision.reason) > 0


# ===========================================================================
# 2. THESIS TRACKING — original vs current
# ===========================================================================


class TestThesisTracking:
    def test_valid_thesis_produces_hold(self):
        """When the original thesis (trending BUY) still matches current
        market state, the manager should HOLD with explicit reasoning."""
        pm = _pm()
        pos = _position(regime="trending", direction=OrderSide.BUY)
        ctx = _market_context(regime=RegimeLabel.TRENDING, slope=0.3, current_price=2555.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.decision == PositionDecision.HOLD
        assert "original thesis remains valid" in decision.reason.lower()

    def test_trend_reversal_triggers_exit(self):
        """When the trend reverses (slope flips negative for a BUY thesis),
        the thesis should be INVALIDATED and EXIT proposed."""
        pm = _pm()
        pos = _position(regime="trending", direction=OrderSide.BUY)
        ctx = _market_context(regime=RegimeLabel.TRENDING, slope=-0.3, current_price=2540.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.INVALIDATED
        assert decision.decision == PositionDecision.EXIT
        assert decision.confidence >= 0.8

    def test_breakout_failure_triggers_exit(self):
        """When a breakout thesis sees breakout state become 'failed',
        the thesis should be INVALIDATED."""
        pm = _pm()
        pos = _position(regime="breakout", direction=OrderSide.BUY)
        ctx = _market_context(
            regime=RegimeLabel.RANGING,
            current_price=2540.0,
        )
        ctx = ctx.model_copy(update={"breakout": BreakoutState(state="failed", direction="up")})
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.INVALIDATED
        assert decision.decision == PositionDecision.EXIT

    def test_momentum_loss_triggers_weakening(self):
        """When momentum persistence drops below threshold, thesis
        should WEAKEN (not immediately invalidated)."""
        pm = _pm()
        pos = _position(regime="trending", direction=OrderSide.BUY)
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.2,
            persist=0.2,  # low persistence
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.WEAKENING
        assert decision.decision == PositionDecision.REDUCE


# ===========================================================================
# 3. HOLD — explicit decision with reasoning
# ===========================================================================


class TestHoldDecision:
    def test_hold_reasons_listed(self):
        """HOLD must explain why."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.HOLD
        assert len(decision.reason) > 0
        assert "valid" in decision.reason.lower() or "regime" in decision.reason.lower()

    def test_hold_is_explicit_not_default(self):
        """HOLD is produced by explicit logic, not by falling through."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.HOLD
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.confidence > 0.0


# ===========================================================================
# 4. MODIFY — trailing stop, within constraints
# ===========================================================================


class TestModifyDecision:
    def test_trailing_stop_activates_when_profitable(self):
        """When price moves in favor by enough, trailing stop should
        propose a stop modification."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2570.0,  # +20 points profit
            stop_loss=2545.0,
            take_profit=2600.0,  # target not hit
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.3,
            atr=5.0,
            current_price=2570.0,
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.MODIFY
        assert decision.proposed_stop_loss is not None
        assert decision.proposed_stop_loss > 2545.0  # stop moved up

    def test_no_modify_when_price_moved_against(self):
        """When price moved against the position, no trailing stop."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2545.0,  # -5 points
            stop_loss=2540.0,  # stop not hit
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.3,
            atr=5.0,
            current_price=2545.0,
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.HOLD
        assert decision.proposed_stop_loss is None

    def test_modify_never_bypasses_risk(self):
        """The Position Manager produces a proposal; it does not execute.
        Verify by checking that the decision is a proposal, not an order."""
        pm = _pm()
        pos = _position(
            current_price=2570.0,
            take_profit=2600.0,  # target not hit
        )
        ctx = _market_context(current_price=2570.0, atr=5.0)
        decision = pm.evaluate(pos, ctx)
        assert isinstance(decision, PositionDecisionModel)
        assert decision.is_actionable()
        # The decision is a proposal — not an OrderRequest
        assert type(decision).__name__ == "PositionDecisionModel"


# ===========================================================================
# 5. REDUCE — thesis weakening
# ===========================================================================


class TestReduceDecision:
    def test_reduce_when_thesis_weakening(self):
        """Multiple weakening signals should produce REDUCE."""
        pm = _pm()
        pos = _position(regime="trending", direction=OrderSide.BUY)
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.05,  # weak trend
            persist=0.25,  # low persistence
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.REDUCE
        assert decision.proposed_volume is not None
        assert decision.proposed_volume < pos.volume

    def test_reduce_volume_calculated_correctly(self):
        """Volume should be reduced by the configured percentage."""
        pm = _pm()
        pos = _position(volume=0.1)
        ctx = _market_context(slope=0.05, persist=0.25)
        decision = pm.evaluate(pos, ctx)
        expected = 0.1 * 0.5  # reduce_volume_pct = 0.5
        assert decision.proposed_volume == pytest.approx(expected)

    def test_reduce_short_position(self):
        """REDUCE works correctly for short positions too."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2555.0,
            regime="trending",
            stop_loss=2565.0,
            take_profit=2545.0,
            invalidation_levels=["2570.0"],  # above entry for SELL
        )
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=-0.03,  # weak downtrend consistent with SELL thesis
            persist=0.25,
            current_price=2555.0,
        )
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.REDUCE

    def test_short_valid_thesis_hold(self):
        """Short with valid thesis should HOLD."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2555.0,
            regime="trending",
            stop_loss=2565.0,
            take_profit=2545.0,
            invalidation_levels=["2570.0"],  # above entry for SELL
        )
        ctx = _market_context(regime=RegimeLabel.TRENDING, slope=-0.3, current_price=2555.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.decision == PositionDecision.HOLD


# ===========================================================================
# 6. EXIT — thesis invalidated
# ===========================================================================
# 6. EXIT — thesis invalidated
# ===========================================================================


class TestExitDecision:
    def test_exit_on_regime_change(self):
        """Regime change from trending to ranging should trigger EXIT."""
        pm = _pm()
        pos = _position(regime="trending", direction=OrderSide.BUY)
        ctx = _market_context(regime=RegimeLabel.RANGING)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EXIT
        assert decision.thesis_status == ThesisStatus.INVALIDATED

    def test_exit_on_stop_hit(self):
        """Price at stop loss should trigger EXIT."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2545.0,
            stop_loss=2545.0,
        )
        ctx = _market_context(current_price=2545.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EXIT

    def test_exit_on_target_hit(self):
        """Price at take profit should trigger EXIT."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.BUY,
            entry_price=2550.0,
            current_price=2565.0,
            stop_loss=2545.0,
            take_profit=2565.0,
        )
        ctx = _market_context(current_price=2565.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EXIT

    def test_exit_short_on_target_hit(self):
        """Short position at target should EXIT."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2545.0,
            stop_loss=2565.0,
            take_profit=2545.0,
        )
        ctx = _market_context(current_price=2545.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EXIT

    def test_exit_includes_invalidation_reasons(self):
        """EXIT decision must include why the thesis was invalidated."""
        pm = _pm()
        pos = _position(regime="trending")
        ctx = _market_context(regime=RegimeLabel.RANGING)
        decision = pm.evaluate(pos, ctx)
        assert len(decision.invalidation_state.get("reasons", [])) > 0


# ===========================================================================
# 7. EMERGENCY EXIT
# ===========================================================================


class TestEmergencyExit:
    def test_kill_switch_triggers_emergency(self):
        """Kill switch engaged should produce EMERGENCY_EXIT."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        risk_ctx = {"kill_switch": True}
        decision = pm.evaluate(pos, ctx, risk_context=risk_ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT
        assert "kill_switch" in decision.reason

    def test_critical_data_triggers_emergency(self):
        """CRITICAL data quality should produce EMERGENCY_EXIT."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(data_quality=DataQualityLevel.CRITICAL)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT
        assert "data_degraded" in decision.reason or "emergency" in decision.reason.lower()

    def test_abnormal_regime_triggers_emergency(self):
        """ABNORMAL regime should produce EMERGENCY_EXIT."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(regime=RegimeLabel.ABNORMAL)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT
        assert decision.confidence == 1.0

    def test_undefined_regime_triggers_emergency(self):
        """UNDEFINED regime should produce EMERGENCY_EXIT."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(regime=RegimeLabel.UNDEFINED)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EMERGENCY_EXIT


# ===========================================================================
# 8. STALE CONTEXT -> NO_ACTION
# ===========================================================================


class TestStaleContext:
    def test_stale_context_produces_no_action(self):
        """Stale context (> max_stale_context_s) should produce NO_ACTION."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(last_tick_age_s=400.0)  # > 300s threshold
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.NO_ACTION
        assert decision.thesis_status == ThesisStatus.UNKNOWN

    def test_fresh_context_does_not_no_action(self):
        """Fresh context should not produce NO_ACTION."""
        pm = _pm()
        pos = _position()
        ctx = _market_context(last_tick_age_s=1.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision != PositionDecision.NO_ACTION


# ===========================================================================
# 9. RECONCILIATION — broker boundary
# ===========================================================================


class TestReconciliation:
    def test_matching_position(self):
        """Matching internal and broker positions should reconcile."""
        internal = _position()
        broker = BrokerPosition(
            ticket="t1",
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=0.1,
            entry_price=2550.0,
            current_price=2555.0,
            stop_loss=2545.0,
            take_profit=2565.0,
            unrealized_pnl=50.0,
            opened_at=_ts(hour=1),
        )
        result = reconcile_position(internal, [broker])
        assert result.matched is True
        assert len(result.mismatches) == 0

    def test_volume_mismatch_detected(self):
        """Volume mismatch should be detected."""
        internal = _position(volume=0.1)
        broker = BrokerPosition(
            ticket="t1",
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=0.2,  # different volume
            entry_price=2550.0,
            current_price=2555.0,
            stop_loss=2545.0,
            take_profit=2565.0,
            unrealized_pnl=50.0,
            opened_at=_ts(hour=1),
        )
        result = reconcile_position(internal, [broker])
        assert result.matched is False
        assert any("volume" in m for m in result.mismatches)

    def test_missing_broker_position_detected(self):
        """Missing broker position should be detected."""
        internal = _position()
        result = reconcile_position(internal, [])
        assert result.matched is False
        assert "no matching broker position" in result.mismatches[0]

    def test_no_direction_side_mismatch(self):
        """BUY internal vs SELL broker should not match."""
        internal = _position(direction=OrderSide.BUY)
        broker = BrokerPosition(
            ticket="t1",
            symbol="XAUUSD",
            side=OrderSide.SELL,  # different direction
            volume=0.1,
            entry_price=2550.0,
            current_price=2555.0,
            stop_loss=2545.0,
            take_profit=2565.0,
            unrealized_pnl=50.0,
            opened_at=_ts(hour=1),
        )
        result = reconcile_position(internal, [broker])
        assert result.matched is False
        assert "no matching broker position" in result.mismatches[0]


# ===========================================================================
# 10. DETERMINISM
# ===========================================================================


class TestDeterminism:
    def test_same_inputs_same_decision(self):
        """Same position + same context should produce the same decision
        structure (action, status, confidence)."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        d1 = pm.evaluate(pos, ctx)
        d2 = pm.evaluate(pos, ctx)
        assert d1.decision == d2.decision
        assert d1.thesis_status == d2.thesis_status
        assert d1.confidence == d2.confidence
        assert d1.reason == d2.reason

    def test_decision_ids_unique(self):
        """Each evaluation should produce a unique decision_id and
        correlation_id."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        d1 = pm.evaluate(pos, ctx)
        d2 = pm.evaluate(pos, ctx)
        assert d1.decision_id != d2.decision_id
        assert d1.correlation_id != d2.correlation_id


# ===========================================================================
# 11. INTEGRATION TEST — full lifecycle
# ===========================================================================


class TestIntegrationLifecycle:
    def test_full_lifecycle(self):
        """Complete scenario: MarketContext -> agents -> synthesis ->
        risk approval -> order -> mock execution -> open position ->
        market changes -> position manager -> thesis weakens -> REDUCE.
        """
        from mt5_platform.agents import (
            AgentContext,
            run_agent_network,
        )
        from mt5_platform.common.enums import OrderSide
        from mt5_platform.risk import RiskContext, RiskEngine

        # Step 1: Build market context
        ctx = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.5,
            atr=5.0,
            roc=0.8,
            persist=0.9,
            current_price=2550.0,
        )

        # Step 2: Run agent network
        agent_ctx = AgentContext(market_context=ctx)
        thesis = run_agent_network(agent_ctx)

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
        risk_ctx = RiskContext(
            account=account,
            proposed_volume=0.1,
        )
        risk_result = risk.evaluate(signal, risk_ctx)
        assert risk_result.approved is True

        # Step 4: Create position
        position = PositionState(
            position_id="pos_int_1",
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

        # Step 5: Market changes — thesis weakens
        pm = _pm()
        ctx_weak = _market_context(
            regime=RegimeLabel.TRENDING,
            slope=0.05,
            persist=0.25,
            current_price=2545.0,
        )
        decision = pm.evaluate(position, ctx_weak)

        # Step 6: Verify thesis weakening
        assert decision.thesis_status in (ThesisStatus.WEAKENING, ThesisStatus.INVALIDATED)
        assert decision.decision in (PositionDecision.REDUCE, PositionDecision.EXIT)
        assert decision.original_thesis_id == thesis.thesis_id
        assert decision.position_id == "pos_int_1"

    def test_reconstructed_from_records(self):
        """Verify that the full decision chain can be reconstructed."""
        pm = _pm()
        pos = _position()
        ctx = _market_context()
        decision = pm.evaluate(pos, ctx)

        # Reconstruct from decision
        assert decision.position_id == "pos_1"
        assert decision.original_thesis_id == "thesis_1"
        assert decision.current_context_id == ctx.context_id
        assert decision.thesis_status is not None
        assert decision.decision is not None
        assert decision.confidence >= 0.0
        assert len(decision.reason) > 0


# ===========================================================================
# 12. SHORT POSITION CORRECTNESS
# ===========================================================================


class TestShortPosition:
    def test_short_stop_hit(self):
        """Short position stop above entry should EXIT."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2565.0,  # above stop
            stop_loss=2565.0,
        )
        ctx = _market_context(current_price=2565.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EXIT

    def test_short_target_hit(self):
        """Short position at target should EXIT."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2545.0,  # at target
            stop_loss=2565.0,
            take_profit=2545.0,
        )
        ctx = _market_context(current_price=2545.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.decision == PositionDecision.EXIT

    def test_short_valid_thesis_hold(self):
        """Short with valid thesis should HOLD."""
        pm = _pm()
        pos = _position(
            direction=OrderSide.SELL,
            entry_price=2560.0,
            current_price=2555.0,
            regime="trending",
            stop_loss=2565.0,
            take_profit=2545.0,
            invalidation_levels=["2570.0"],  # above entry for SELL
        )
        ctx = _market_context(regime=RegimeLabel.TRENDING, slope=-0.3, current_price=2555.0)
        decision = pm.evaluate(pos, ctx)
        assert decision.thesis_status == ThesisStatus.VALID
        assert decision.decision == PositionDecision.HOLD
