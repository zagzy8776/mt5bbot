"""Post-trade review engine (Phase E).

Compares expected vs actual for every completed trade and produces
a structured PostTradeReview. Facts are immutable; interpretation
is explicitly a hypothesis.
"""

from __future__ import annotations

from mt5_platform.common.enums import ReviewOutcome, TradeCause
from mt5_platform.learning.models import (
    Counterfactual,
    PostTradeReview,
    TradeOutcome,
)


class PostTradeReviewEngine:
    """Produces structured post-trade reviews from TradeOutcome."""

    def review(self, outcome: TradeOutcome) -> PostTradeReview:
        thesis = outcome.thesis_snapshot or {}
        expected_direction = thesis.get("direction", "")
        expected_regime = thesis.get("regime", "")
        expected_thesis = " | ".join(thesis.get("reasons", []))
        expected_risk_pct = 0.0
        expected_target = thesis.get("take_profit")
        expected_invalidation = " | ".join(thesis.get("invalidation_levels", []))

        actual_exit = outcome.exit
        actual_pnl = outcome.realized_pnl
        actual_return_pct = outcome.return_pct
        actual_mae = outcome.mae
        actual_mfe = outcome.mfe
        actual_duration_s = outcome.duration_s
        actual_exit_reason = (
            outcome.cause_class.value if outcome.cause_class else TradeCause.UNKNOWN.value
        )

        direction_correct = self._check_direction(expected_direction, outcome)
        regime_matched = self._check_regime(expected_regime, outcome)
        thesis_invalidated_by = self._find_invalidation_reasons(outcome)

        outcome_type = self._classify_outcome(outcome)
        cause_class = outcome.cause_class or TradeCause.UNKNOWN

        interpretation = self._build_interpretation(
            outcome, direction_correct, regime_matched, thesis_invalidated_by
        )
        lessons_proposed = self._propose_lessons(outcome, outcome_type, cause_class)

        return PostTradeReview(
            trade_id=outcome.trade_id,
            thesis_id=outcome.thesis_id,
            context_id=outcome.context_id,
            expected_direction=expected_direction,
            expected_regime=expected_regime,
            expected_thesis=expected_thesis,
            expected_risk_pct=expected_risk_pct,
            expected_target=expected_target,
            expected_invalidation=expected_invalidation,
            actual_exit=actual_exit,
            actual_pnl=actual_pnl,
            actual_return_pct=actual_return_pct,
            actual_mae=actual_mae,
            actual_mfe=actual_mfe,
            actual_duration_s=actual_duration_s,
            actual_exit_reason=actual_exit_reason,
            direction_correct=direction_correct,
            regime_matched=regime_matched,
            thesis_invalidated_by=thesis_invalidated_by,
            outcome=outcome_type,
            cause_class=cause_class,
            interpretation=interpretation,
            lessons_proposed=lessons_proposed,
        )

    def _check_direction(self, expected: str, outcome: TradeOutcome) -> bool | None:
        if not expected:
            return None
        actual_dir = outcome.direction.lower()
        return expected.lower() == actual_dir

    def _check_regime(self, expected: str, outcome: TradeOutcome) -> bool | None:
        if not expected:
            return None
        actual_regime = (outcome.thesis_snapshot or {}).get("regime", "")
        return expected.lower() == actual_regime.lower() if actual_regime else None

    def _find_invalidation_reasons(self, outcome: TradeOutcome) -> list[str]:
        reasons: list[str] = []
        for decision in outcome.position_decisions:
            if decision.get("thesis_status") == "invalidated":
                inv = decision.get("invalidation_state", {}).get("reasons", [])
                reasons.extend(inv)
        if outcome.cause_class == TradeCause.STOP_HIT:
            reasons.append("stop_hit")
        if outcome.cause_class == TradeCause.TARGET_HIT:
            reasons.append("target_hit")
        if outcome.cause_class == TradeCause.REGIME_CHANGE:
            reasons.append("regime_change")
        return list(set(reasons))

    def _classify_outcome(self, outcome: TradeOutcome) -> ReviewOutcome:
        if outcome.realized_pnl > 0:
            return ReviewOutcome.STATISTICAL_WIN
        if outcome.realized_pnl < 0:
            if outcome.mae_pct > 5.0 and outcome.mfe_pct > 3.0:
                return ReviewOutcome.STATISTICAL_LOSS
            return ReviewOutcome.LOSS
        return ReviewOutcome.BREAKEVEN

    def _build_interpretation(
        self,
        outcome: TradeOutcome,
        direction_correct: bool | None,
        regime_matched: bool | None,
        invalidation_reasons: list[str],
    ) -> str:
        parts: list[str] = []
        if direction_correct is False:
            parts.append("Directional call was incorrect")
        if regime_matched is False:
            parts.append("Regime did not match expectation")
        if "stop_hit" in invalidation_reasons:
            parts.append("Stop loss was triggered")
        if "regime_change" in invalidation_reasons:
            parts.append("Regime transition occurred during trade")
        if not parts:
            parts.append("Trade outcome within expected statistical range")
        return "; ".join(parts)

    def _propose_lessons(
        self, outcome: TradeOutcome, outcome_type: ReviewOutcome, cause_class: TradeCause
    ) -> list[str]:
        lessons: list[str] = []
        if outcome_type == ReviewOutcome.STATISTICAL_LOSS:
            lessons.append("valid_trade_adverse_outcome — no automatic action")
        if cause_class == TradeCause.REGIME_CHANGE:
            lessons.append("regime_detection_timing — review regime transition signals")
        if cause_class == TradeCause.DATA_DEGRADED:
            lessons.append("data_quality_gate — verify data quality before entry")
        if outcome.mae_pct > 10.0:
            lessons.append("stop_placement_review — examine stop distance relative to ATR")
        return lessons

    def generate_counterfactual(
        self,
        outcome: TradeOutcome,
        scenario: str,
        *,
        simulated_exit: float | None = None,
        simulated_entry: float | None = None,
        assumptions: list[str] | None = None,
    ) -> Counterfactual:
        entry = outcome.entry
        exit_price = simulated_exit if simulated_exit is not None else (outcome.exit or entry)
        direction = 1.0 if outcome.direction == "buy" else -1.0
        simulated_pnl = direction * (exit_price - entry)
        simulated_return = (simulated_pnl / entry * 100.0) if entry != 0 else 0.0

        # Simplified MAE/MFE for counterfactual
        if scenario in ("exited_earlier", "held_longer"):
            mae = outcome.mae
            mfe = outcome.mfe
        else:
            mae = outcome.mae * 0.8
            mfe = outcome.mfe * 0.8

        return Counterfactual(
            trade_id=outcome.trade_id,
            scenario=scenario,
            simulated_pnl=simulated_pnl,
            simulated_return_pct=simulated_return,
            simulated_mae=mae,
            simulated_mfe=mfe,
            assumptions=assumptions or [],
            is_simulation=True,
        )


__all__ = ["PostTradeReviewEngine"]
