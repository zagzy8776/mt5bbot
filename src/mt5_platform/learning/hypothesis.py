"""Hypothesis registry (Phase E).

Stores learnable hypotheses from trade experience. A hypothesis must
be validated before it can influence system behavior.

No automatic mutation. A hypothesis can only become a configuration
change through explicit approval.
"""

from __future__ import annotations

from typing import Any

from mt5_platform.common.enums import (
    ConfigurationChangeType,
    LessonStatus,
)
from mt5_platform.learning.models import (
    Lesson,
    LessonEvidence,
)


class HypothesisRegistry:
    """Immutable-first hypothesis store.

    A hypothesis progresses through states:
        PROPOSED -> UNDER_VALIDATION -> VALIDATED -> (approved for config change)
        PROPOSED -> UNDER_VALIDATION -> REJECTED
        VALIDATED -> STALE (if superseded by newer evidence)
        VALIDATED -> SUPERSEDED (if replaced by better hypothesis)
    """

    def __init__(self) -> None:
        self._hypotheses: dict[str, Lesson] = {}

    def propose(self, lesson: Lesson) -> str:
        lesson_id = (
            lesson.lesson_id
            or __import__(
                "mt5_platform.common.ids", fromlist=["new_execution_id"]
            ).new_execution_id()
        )
        lesson.lesson_id = lesson_id
        lesson.status = LessonStatus.PROPOSED
        self._hypotheses[lesson_id] = lesson
        return lesson_id

    def add_evidence(self, lesson_id: str, evidence: LessonEvidence) -> None:
        lesson = self._hypotheses.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        lesson.evidence.append(evidence)
        lesson.sample_size = len(lesson.evidence)
        self._recalculate(lesson)

    def start_validation(self, lesson_id: str) -> None:
        lesson = self._hypotheses.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        lesson.status = LessonStatus.UNDER_VALIDATION

    def validate(self, lesson_id: str) -> None:
        lesson = self._hypotheses.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        lesson.status = LessonStatus.VALIDATED
        lesson.validated_at = __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()

    def reject(self, lesson_id: str, reason: str = "") -> None:
        lesson = self._hypotheses.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        lesson.status = LessonStatus.REJECTED
        lesson.rejected_at = __import__(
            "mt5_platform.common.events", fromlist=["utc_now"]
        ).utc_now()

    def mark_stale(self, lesson_id: str) -> None:
        lesson = self._hypotheses.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        lesson.status = LessonStatus.STALE

    def supersede(self, lesson_id: str, new_lesson_id: str) -> None:
        lesson = self._hypotheses.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        lesson.status = LessonStatus.SUPERSEDED
        lesson.superseded_by = new_lesson_id

    def propose_configuration_change(
        self,
        lesson_id: str,
        change_type: ConfigurationChangeType,
        proposed_change: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Only VALIDATED lessons can propose configuration changes.

        Returns a configuration proposal dict, or None if not validated.
        The proposal is NOT automatically applied.
        """
        lesson = self._hypotheses.get(lesson_id)
        if lesson is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        if lesson.status != LessonStatus.VALIDATED:
            return None
        return {
            "lesson_id": lesson_id,
            "hypothesis_id": lesson.hypothesis_id,
            "change_type": change_type.value,
            "proposed_change": proposed_change,
            "confidence": lesson.confidence,
            "evidence_quality": lesson.evidence_quality,
            "sample_size": lesson.sample_size,
            "status": "pending_approval",  # must be explicitly approved
        }

    def get(self, lesson_id: str) -> Lesson | None:
        return self._hypotheses.get(lesson_id)

    def list_by_status(self, status: LessonStatus) -> list[Lesson]:
        return [h for h in self._hypotheses.values() if h.status == status]

    def _recalculate(self, lesson: Lesson) -> None:
        if lesson.sample_size == 0:
            return
        supports = sum(1 for e in lesson.evidence if e.supports)
        lesson.confidence = supports / lesson.sample_size
        if lesson.sample_size >= 100:
            lesson.evidence_quality = "strong"
        elif lesson.sample_size >= 30:
            lesson.evidence_quality = "moderate"
        elif lesson.sample_size >= 10:
            lesson.evidence_quality = "weak"
        else:
            lesson.evidence_quality = "insufficient"


__all__ = ["HypothesisRegistry"]
