"""Position Manager — thesis tracking and position intelligence (Phase D).

The Position Manager is the only component that evaluates open positions
against current market evidence. It produces PositionDecision objects
that flow through RiskEngine -> OrderManager -> Execution.

It does NOT execute directly. Every decision is explicit and traceable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import (
    DataQualityLevel,
    InvalidationReason,
    OrderSide,
    PositionDecision,
    RegimeLabel,
    ThesisStatus,
)
from mt5_platform.common.events import AuditEvent, utc_now
from mt5_platform.position.models import (
    POSITION_DECISION_EMITTED,
    POSITION_THESIS_INVALIDATED,
    PositionDecisionModel,
    PositionManagerAudit,
    PositionManagerConfig,
    PositionState,
    ThesisSnapshot,
)


class PositionManager:
    """Evaluates open positions against current market evidence.

    For each position, compares the original thesis snapshot against
    the current MarketContext and historical evidence to produce a
    PositionDecision.

    Flow:
        PositionManager -> PositionDecision
                        -> RiskEngine (validation)
                        -> OrderManager (execution)

    Never bypasses Risk or OrderManager.
    """

    def __init__(
        self,
        *,
        config: PositionManagerConfig | None = None,
    ) -> None:
        self.config = config or PositionManagerConfig()

    def evaluate(
        self,
        position: PositionState,
        market_context: Any,  # MarketContext
        *,
        historical_evidence: dict[str, Any] | None = None,
        risk_context: dict[str, Any] | None = None,
    ) -> PositionDecisionModel:
        """Single evaluation cycle for one position.

        Returns a PositionDecisionModel that must pass through RiskEngine
        before any order is created.
        """
        now = utc_now()
        ctx = market_context

        # Build thesis snapshot if not present
        thesis_snap = position.thesis_snapshot or self._build_thesis_snapshot(position, ctx)

        # Check emergency conditions first
        emergency = self._check_emergency(position, ctx, risk_context)
        if emergency:
            decision = self._emergency_decision(position, thesis_snap, ctx, emergency)
            self._emit_audit(position, thesis_snap, decision, now, emergency=True)
            return decision

        # Check context staleness
        if self._is_context_stale(ctx, now):
            decision = self._no_action_decision(position, thesis_snap, ctx, "stale context")
            self._emit_audit(position, thesis_snap, decision, now)
            return decision

        # Evaluate thesis validity
        thesis_status, invalidation_reasons = self._evaluate_thesis(
            position, thesis_snap, ctx, historical_evidence
        )

        # Produce decision based on thesis status
        if thesis_status == ThesisStatus.INVALIDATED:
            decision = self._exit_decision(position, thesis_snap, ctx, invalidation_reasons)
        elif thesis_status == ThesisStatus.WEAKENING:
            decision = self._reduce_decision(position, thesis_snap, ctx, invalidation_reasons)
        elif thesis_status == ThesisStatus.VALID:
            # Check for trailing stop / modify opportunity
            decision = self._hold_or_modify_decision(
                position, thesis_snap, ctx, historical_evidence
            )
        else:
            decision = self._no_action_decision(position, thesis_snap, ctx, "insufficient context")

        self._emit_audit(position, thesis_snap, decision, now, invalidation_reasons)
        return decision

    def _build_thesis_snapshot(self, position: PositionState, ctx: Any) -> ThesisSnapshot:
        return ThesisSnapshot(
            thesis_id=position.thesis_id,
            instrument=position.instrument,
            direction=position.direction,
            regime=ctx.regime.value if ctx.regime else None,
            confidence=0.0,  # would be filled from original thesis
            entry=position.entry_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
        )

    def _check_emergency(
        self,
        position: PositionState,
        ctx: Any,
        risk_context: dict[str, Any] | None,
    ) -> list[InvalidationReason] | None:
        reasons: list[InvalidationReason] = []

        # Kill switch
        if self.config.kill_switch_check and risk_context:
            if risk_context.get("kill_switch"):
                reasons.append(InvalidationReason.KILL_SWITCH)

        # Data quality critical
        if ctx.data_quality.level == DataQualityLevel.CRITICAL:
            reasons.append(InvalidationReason.DATA_DEGRADED)

        # Abnormal regime
        if ctx.regime in (RegimeLabel.ABNORMAL, RegimeLabel.UNDEFINED):
            reasons.append(InvalidationReason.REGIME_CHANGE)

        return reasons if reasons else None

    def _is_context_stale(self, ctx: Any, now: datetime) -> bool:
        if ctx.data_quality.last_tick_age_s is None:
            return True
        return ctx.data_quality.last_tick_age_s > self.config.max_stale_context_s

    def _evaluate_thesis(
        self,
        position: PositionState,
        thesis: ThesisSnapshot,
        ctx: Any,
        historical_evidence: dict[str, Any] | None,
    ) -> tuple[ThesisStatus, list[InvalidationReason]]:
        reasons: list[InvalidationReason] = []

        # Stop already hit
        if position.stop_loss is not None and position.current_price is not None:
            if position.direction == OrderSide.BUY and position.current_price <= position.stop_loss:
                reasons.append(InvalidationReason.STOP_HIT)
            elif (
                position.direction == OrderSide.SELL
                and position.current_price >= position.stop_loss
            ):
                reasons.append(InvalidationReason.STOP_HIT)

        # Target hit
        if position.take_profit is not None and position.current_price is not None:
            if (
                position.direction == OrderSide.BUY
                and position.current_price >= position.take_profit
            ):
                reasons.append(InvalidationReason.TARGET_HIT)
            elif (
                position.direction == OrderSide.SELL
                and position.current_price <= position.take_profit
            ):
                reasons.append(InvalidationReason.TARGET_HIT)

        # Check regime consistency
        if thesis.regime and ctx.regime:
            if thesis.regime != ctx.regime.value:
                # Regime changed from thesis
                if thesis.regime == "trending" and ctx.regime == RegimeLabel.RANGING:
                    reasons.append(InvalidationReason.TREND_REVERSAL)
                elif thesis.regime == "breakout" and ctx.regime != RegimeLabel.BREAKOUT:
                    reasons.append(InvalidationReason.BREAKOUT_FAILURE)
                else:
                    reasons.append(InvalidationReason.REGIME_CHANGE)

        # Check trend consistency
        if thesis.regime == "trending" and ctx.trend and ctx.trend.slope_per_bar_pct is not None:
            expected_up = thesis.direction == OrderSide.BUY
            actual_up = ctx.trend.slope_per_bar_pct > 0
            if expected_up != actual_up:
                reasons.append(InvalidationReason.TREND_REVERSAL)

        # Check momentum
        if ctx.momentum and ctx.momentum.persistence is not None:
            if ctx.momentum.persistence < 0.3:
                reasons.append(InvalidationReason.MOMENTUM_LOSS)

        # Check breakout state
        if thesis.regime == "breakout" and ctx.breakout:
            if ctx.breakout.state == "failed":
                reasons.append(InvalidationReason.BREAKOUT_FAILURE)

        # Check volatility spike
        if ctx.volatility and ctx.volatility.atr_to_median is not None:
            if ctx.volatility.atr_to_median > 2.5:
                reasons.append(InvalidationReason.VOLATILITY_SPIKE)

        # Check invalidation levels from thesis
        if thesis.invalidation_levels and position.current_price is not None:
            for level_str in thesis.invalidation_levels:
                try:
                    level = float(level_str)
                    if position.direction == OrderSide.BUY and position.current_price <= level:
                        reasons.append(InvalidationReason.TREND_REVERSAL)
                    elif position.direction == OrderSide.SELL and position.current_price >= level:
                        reasons.append(InvalidationReason.TREND_REVERSAL)
                except ValueError:
                    pass

        # Historical evidence degradation
        if historical_evidence:
            overall = historical_evidence.get("overall", {})
            if overall.get("evidence_quality") == "insufficient":
                pass  # no historical evidence, not a negative
            elif overall.get("expectancy") is not None and float(overall.get("expectancy", 0)) < 0:
                reasons.append(InvalidationReason.REGIME_CHANGE)

        # Determine thesis status
        if reasons:
            if InvalidationReason.STOP_HIT in reasons or InvalidationReason.TARGET_HIT in reasons:
                return ThesisStatus.INVALIDATED, reasons
            if (
                len(reasons) >= 2
                or InvalidationReason.TREND_REVERSAL in reasons
                or InvalidationReason.BREAKOUT_FAILURE in reasons
            ):
                return ThesisStatus.INVALIDATED, reasons
            return ThesisStatus.WEAKENING, reasons

        return ThesisStatus.VALID, []

    def _hold_or_modify_decision(
        self,
        position: PositionState,
        thesis: ThesisSnapshot,
        ctx: Any,
        historical_evidence: dict[str, Any] | None,
    ) -> PositionDecision:
        # Check for trailing stop opportunity
        proposed_stop = position.stop_loss
        if position.current_price is not None and position.stop_loss is not None:
            atr = ctx.volatility.atr if ctx.volatility else None
            if atr and atr > 0:
                if position.direction == OrderSide.BUY:
                    # Price moved up — trail stop up
                    profit_pct = (
                        position.current_price - position.entry_price
                    ) / position.entry_price
                    if profit_pct >= self.config.trail_activation_pct / 100.0:
                        new_stop = (
                            position.current_price - atr * self.config.trail_distance_atr_mult
                        )
                        if new_stop > position.stop_loss:
                            proposed_stop = new_stop
                else:
                    # Price moved down — trail stop down
                    profit_pct = (
                        position.entry_price - position.current_price
                    ) / position.entry_price
                    if profit_pct >= self.config.trail_activation_pct / 100.0:
                        new_stop = (
                            position.current_price + atr * self.config.trail_distance_atr_mult
                        )
                        if new_stop < position.stop_loss:
                            proposed_stop = new_stop

        if proposed_stop != position.stop_loss:
            return PositionDecisionModel(
                position_id=position.position_id,
                original_thesis_id=thesis.thesis_id,
                current_context_id=ctx.context_id,
                thesis_status=ThesisStatus.VALID,
                decision=PositionDecision.MODIFY,
                confidence=0.7,
                proposed_stop_loss=proposed_stop,
                reason=f"trailing stop activated: {position.stop_loss} -> {proposed_stop}",
                risk_state={"trailing": True},
            )

        # HOLD with explicit reasoning
        hold_reasons = [
            "original thesis remains valid",
            f"regime: {ctx.regime.value if ctx.regime else 'unknown'}",
            f"trend: {ctx.trend.slope_per_bar_pct:.4f}%/bar"
            if ctx.trend and ctx.trend.slope_per_bar_pct
            else "trend: unknown",
        ]
        if historical_evidence:
            overall = historical_evidence.get("overall", {})
            if overall.get("evidence_quality") != "insufficient":
                hold_reasons.append(f"historical expectancy: {overall.get('expectancy', 'N/A')}")

        return PositionDecisionModel(
            position_id=position.position_id,
            original_thesis_id=thesis.thesis_id,
            current_context_id=ctx.context_id,
            thesis_status=ThesisStatus.VALID,
            decision=PositionDecision.HOLD,
            confidence=0.8,
            reason="; ".join(hold_reasons),
            risk_state={"within_limits": True},
        )

    def _reduce_decision(
        self,
        position: PositionState,
        thesis: ThesisSnapshot,
        ctx: Any,
        reasons: list[InvalidationReason],
    ) -> PositionDecision:
        reduce_vol = position.volume * self.config.reduce_volume_pct
        return PositionDecisionModel(
            position_id=position.position_id,
            original_thesis_id=thesis.thesis_id,
            current_context_id=ctx.context_id,
            thesis_status=ThesisStatus.WEAKENING,
            decision=PositionDecision.REDUCE,
            confidence=0.6,
            proposed_volume=reduce_vol,
            reason=(
                f"thesis weakening: {', '.join(r.value for r in reasons)}; "
                f"reducing volume by {self.config.reduce_volume_pct:.0%}"
            ),
            invalidation_state={"reasons": [r.value for r in reasons]},
        )

    def _exit_decision(
        self,
        position: PositionState,
        thesis: ThesisSnapshot,
        ctx: Any,
        reasons: list[InvalidationReason],
    ) -> PositionDecision:
        return PositionDecisionModel(
            position_id=position.position_id,
            original_thesis_id=thesis.thesis_id,
            current_context_id=ctx.context_id,
            thesis_status=ThesisStatus.INVALIDATED,
            decision=PositionDecision.EXIT,
            confidence=0.9,
            reason=f"thesis invalidated: {', '.join(r.value for r in reasons)}",
            invalidation_state={"reasons": [r.value for r in reasons]},
        )

    def _emergency_decision(
        self,
        position: PositionState,
        thesis: ThesisSnapshot,
        ctx: Any,
        reasons: list[InvalidationReason],
    ) -> PositionDecision:
        return PositionDecisionModel(
            position_id=position.position_id,
            original_thesis_id=thesis.thesis_id,
            current_context_id=ctx.context_id,
            thesis_status=ThesisStatus.INVALIDATED,
            decision=PositionDecision.EMERGENCY_EXIT,
            confidence=1.0,
            reason=f"emergency: {', '.join(r.value for r in reasons)}",
            invalidation_state={"reasons": [r.value for r in reasons], "emergency": True},
        )

    def _no_action_decision(
        self,
        position: PositionState,
        thesis: ThesisSnapshot,
        ctx: Any,
        reason: str,
    ) -> PositionDecision:
        return PositionDecisionModel(
            position_id=position.position_id,
            original_thesis_id=thesis.thesis_id,
            current_context_id=ctx.context_id,
            thesis_status=ThesisStatus.UNKNOWN,
            decision=PositionDecision.NO_ACTION,
            confidence=0.0,
            reason=reason,
        )

    def _emit_audit(
        self,
        position: PositionState,
        thesis: ThesisSnapshot,
        decision: PositionDecision,
        now: datetime,
        invalidation_reasons: list[InvalidationReason] | None = None,
        emergency: bool = False,
    ) -> None:
        audit = PositionManagerAudit(
            position_id=position.position_id,
            thesis_id=thesis.thesis_id,
            thesis_status=decision.thesis_status,
            decision=decision.decision,
            confidence=decision.confidence,
            reason=decision.reason,
            correlation_id=decision.correlation_id,
            invalidation_reasons=invalidation_reasons or [],
        )
        audit_log.emit(
            AuditEvent(
                component="position_manager",
                event_type=POSITION_THESIS_INVALIDATED
                if decision.thesis_status == ThesisStatus.INVALIDATED
                else POSITION_DECISION_EMITTED,
                severity="warning"
                if decision.decision in (PositionDecision.EXIT, PositionDecision.EMERGENCY_EXIT)
                else "info",
                symbol=position.instrument,
                correlation_id=decision.correlation_id,
                payload=audit.model_dump(mode="json"),
            )
        )


__all__ = ["PositionManager", "PositionManagerConfig"]
