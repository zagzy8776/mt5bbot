"""In-memory historical outcome ledger.

Append-only store of HistoricalOutcome records. In a production system this
would be backed by SQL; for Phase C the in-memory store is sufficient and
keeps the engine testable without database fixtures.

Chronological filtering is a first-class operation: every read must be
able to bound results to a time window so the evidence engine never
accidentally sees the future.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mt5_platform.historical.models import HistoricalOutcome


class InMemoryHistoricalLedger:
    """Append-only ledger of closed trade outcomes.

    All reads can be bounded by an `as_of` timestamp. The engine must pass
    `as_of` to every read so that future data never leaks into a
    historical query.
    """

    def __init__(self) -> None:
        self._outcomes: list[HistoricalOutcome] = []

    def record(self, outcome: HistoricalOutcome) -> None:
        """Append one outcome. Order is preserved (insertion order = time order
        for well-behaved callers)."""
        self._outcomes.append(outcome)

    def record_many(self, outcomes: list[HistoricalOutcome]) -> None:
        for o in outcomes:
            self.record(o)

    def __len__(self) -> int:
        return len(self._outcomes)

    def query(
        self,
        *,
        instrument: str | None = None,
        strategy: str | None = None,
        regime: str | None = None,
        session: str | None = None,
        timeframe: str | None = None,
        direction: str | None = None,
        as_of: datetime | None = None,
    ) -> list[HistoricalOutcome]:
        """Return outcomes matching the filters, chronologically bounded by as_of.

        `as_of` is the strict upper bound: only outcomes with timestamp <= as_of
        are returned. This is the primary defense against future-data leakage.
        """
        results: list[HistoricalOutcome] = []
        for o in self._outcomes:
            if as_of is not None and o.timestamp > as_of:
                continue
            if instrument is not None and o.instrument != instrument:
                continue
            if strategy is not None and o.strategy != strategy:
                continue
            if direction is not None and o.direction.value != direction:
                continue
            if regime is not None:
                if o.features is None or o.features.regime is None:
                    continue
                if o.features.regime.value != regime:
                    continue
            if session is not None:
                if o.features is None or o.features.session != session:
                    continue
            if timeframe is not None:
                if o.features is None or o.features.timeframe != timeframe:
                    continue
            results.append(o)
        return results

    def all(self) -> list[HistoricalOutcome]:
        return list(self._outcomes)
