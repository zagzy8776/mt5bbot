"""Completed outcome -> review -> lesson -> hypothesis. Explicitly NOT a closed learning loop.

Nothing here can change trading configuration: reviews are recorded, lessons are *proposed* as
observations with no proposed configuration change, and promotion stays a deliberate step
(research validation -> explicit configuration version). This boundary is what keeps "learning"
from silently rewriting live behaviour.
"""

from __future__ import annotations

from typing import Any

from mt5_platform.historical.models import HistoricalOutcome
from mt5_platform.learning import (
    DecisionMemory,
    DecisionMemoryRecord,
    HypothesisRegistry,
    Lesson,
    PostTradeReview,
    PostTradeReviewEngine,
)
from mt5_platform.learning.models import TradeOutcome

# Lessons proposed automatically are observations only: never a configuration change.
LESSON_CONFIDENCE = 0.2


def to_trade_outcome(outcome: HistoricalOutcome) -> TradeOutcome:
    """Faithful mapping to the learning model: absent values stay absent."""
    features = outcome.features
    regime = ""
    if features is not None and features.regime is not None:
        regime = features.regime.value
    return TradeOutcome(
        trade_id=outcome.trade_id,
        instrument=outcome.instrument,
        strategy=outcome.strategy,
        direction=outcome.direction.value,
        entry=outcome.entry,
        exit=outcome.exit_price,
        stop_loss=outcome.initial_stop_loss,
        take_profit=outcome.initial_take_profit,
        realized_pnl=outcome.realized_pnl,
        return_pct=outcome.return_pct,
        mae=outcome.mae,
        mfe=outcome.mfe,
        mae_pct=outcome.mae_pct,
        mfe_pct=outcome.mfe_pct,
        duration_s=outcome.duration_s,
        exit_reason=outcome.exit_reason,
        cause_class=outcome.cause_class,
        opened_at=outcome.timestamp,
        closed_at=outcome.exit_time,
        regime=regime,
        context_id=outcome.entry_context_id,
        thesis_id=outcome.thesis_id,
        order_id=outcome.order_id,
        agent_opinions=list(outcome.agent_opinions),
        thesis_snapshot=dict(outcome.thesis_snapshot),
        risk_decision=dict(outcome.risk_decision),
    )


class OutcomeLearningPipeline:
    """Records the post-trade review and proposes observations; applies nothing."""

    def __init__(
        self,
        *,
        memory: DecisionMemory | None = None,
        review_engine: PostTradeReviewEngine | None = None,
        hypotheses: HypothesisRegistry | None = None,
    ) -> None:
        self.memory = memory if memory is not None else DecisionMemory()
        self.review_engine = review_engine if review_engine is not None else PostTradeReviewEngine()
        self.hypotheses = hypotheses if hypotheses is not None else HypothesisRegistry()
        self.reviews: dict[str, PostTradeReview] = {}
        self.lesson_ids: list[str] = []
        self.reviewed_trade_ids: list[str] = []
        self.errors: int = 0
        self.last_error: str | None = None

    def record(self, outcome: HistoricalOutcome, *, intelligence: Any | None = None) -> Any:
        """Post-trade review, decision memory and lesson proposals. Never raises."""
        try:
            trade = to_trade_outcome(outcome)
            review = self.review_engine.review(trade)
            self.reviews[outcome.trade_id] = review
            self.reviewed_trade_ids.append(outcome.trade_id)
            record = DecisionMemoryRecord(
                trade_id=outcome.trade_id,
                thesis_id=outcome.thesis_id,
                context_id=outcome.entry_context_id,
                correlation_id=outcome.broker_ticket or outcome.trade_id,
                market_context_snapshot=dict(outcome.regime_snapshot),
                agent_opinions=list(outcome.agent_opinions),
                historical_evidence=dict(outcome.evidence.get("historical_evidence", {}) or {}),
                synthesis_decision=dict(outcome.thesis_snapshot),
                risk_decision=dict(outcome.risk_decision),
                order={
                    "order_id": outcome.order_id,
                    "entry": outcome.entry,
                    "volume": outcome.entry_volume,
                    "stop_loss": outcome.initial_stop_loss,
                    "take_profit": outcome.initial_take_profit,
                },
                execution={
                    "exit_price": outcome.exit_price,
                    "exit_cause": outcome.exit_cause.value,
                    "exit_cause_source": outcome.exit_cause_source,
                    "realized_pnl": outcome.realized_pnl,
                    "realized_pnl_source": outcome.evidence.get("realized_pnl_source"),
                    "r_multiple": outcome.r_multiple,
                },
                position_management=[leg.model_dump(mode="json") for leg in outcome.legs],
                outcome=trade,
                review=review,
                lessons=[],
            )
            self.memory.store(record)
            for statement in review.lessons_proposed:
                lesson_id = self.hypotheses.propose(
                    Lesson(
                        statement=statement,
                        supporting_trade_ids=[outcome.trade_id],
                        sample_size=1,
                        confidence=LESSON_CONFIDENCE,
                        evidence_quality="insufficient",
                        # No proposed change: a hypothesis cannot alter behaviour by itself.
                        proposed_change_type=None,
                        proposed_change={},
                    )
                )
                self.lesson_ids.append(lesson_id)
            if intelligence is not None:
                self._notify_intelligence(intelligence, outcome)
            return review
        except Exception as exc:  # learning must never break recording or trading
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    @staticmethod
    def _notify_intelligence(intelligence: Any, outcome: HistoricalOutcome) -> None:
        """Feed the intelligence layer's own memory when that optional layer is active."""
        try:
            from mt5_platform.common.events import StrategySignal

            intelligence.record_completed_trade(
                signal=StrategySignal(
                    symbol=outcome.instrument,
                    direction=outcome.direction,
                    entry=outcome.entry,
                    stop_loss=outcome.initial_stop_loss,
                    take_profit=outcome.initial_take_profit,
                    strategy_name=outcome.strategy,
                ),
                entry=outcome.entry,
                exit_price=outcome.exit_price if outcome.exit_price is not None else outcome.entry,
                volume=outcome.entry_volume or 0.0,
                pnl=outcome.realized_pnl,
            )
        except Exception:
            return  # diagnostics only: never let it break the pipeline

    def stats(self) -> dict[str, Any]:
        return {
            "reviews": len(self.reviews),
            "lessons_proposed": len(self.lesson_ids),
            "memory_records": len(self.memory.all()),
            "hypotheses": len(getattr(self.hypotheses, "_hypotheses", {})),
            "errors": self.errors,
            "last_error": self.last_error,
        }
