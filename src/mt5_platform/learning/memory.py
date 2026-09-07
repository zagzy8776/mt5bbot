"""Decision memory (Phase E).

Stores complete decision chains from MarketContext to Outcome.
Every query is bounded by an as_of timestamp to prevent future-data
leakage.
"""

from __future__ import annotations

from datetime import datetime

from mt5_platform.learning.models import DecisionMemoryRecord


class MemoryQuery:
    """Query for decision memory with temporal boundary.

    `as_of` is the strict upper bound: only records with created_at <= as_of
    are returned. This is the primary defense against memory contamination.
    """

    def __init__(
        self,
        *,
        as_of: datetime | None = None,
        trade_id: str | None = None,
        thesis_id: str | None = None,
        instrument: str | None = None,
        strategy: str | None = None,
        min_confidence: float | None = None,
        outcome_type: str | None = None,
    ) -> None:
        self.as_of = as_of
        self.trade_id = trade_id
        self.thesis_id = thesis_id
        self.instrument = instrument
        self.strategy = strategy
        self.min_confidence = min_confidence
        self.outcome_type = outcome_type


class DecisionMemory:
    """Append-only decision memory with temporal boundaries."""

    def __init__(self) -> None:
        self._records: dict[str, DecisionMemoryRecord] = {}

    def store(self, record: DecisionMemoryRecord) -> None:
        self._records[record.memory_id] = record

    def query(self, q: MemoryQuery) -> list[DecisionMemoryRecord]:
        results = []
        for record in self._records.values():
            if q.as_of is not None and record.created_at > q.as_of:
                continue
            if q.trade_id is not None and record.trade_id != q.trade_id:
                continue
            if q.thesis_id is not None and record.thesis_id != q.thesis_id:
                continue
            if q.instrument is not None:
                outcome = record.outcome
                if outcome and outcome.instrument != q.instrument:
                    continue
            if q.strategy is not None:
                outcome = record.outcome
                if outcome and outcome.strategy != q.strategy:
                    continue
            if q.min_confidence is not None:
                confidence = record.synthesis_decision.get("confidence", 0.0)
                if confidence < q.min_confidence:
                    continue
            if q.outcome_type is not None:
                review = record.review
                if review and review.outcome.value != q.outcome_type:
                    continue
            results.append(record)
        return results

    def get(self, memory_id: str) -> DecisionMemoryRecord | None:
        return self._records.get(memory_id)

    def get_by_trade_id(self, trade_id: str) -> DecisionMemoryRecord | None:
        for record in self._records.values():
            if record.trade_id == trade_id:
                return record
        return None

    def all(self) -> list[DecisionMemoryRecord]:
        return list(self._records.values())
