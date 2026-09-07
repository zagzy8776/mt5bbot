"""Historical Evidence Engine (Phase C).

The engine is the only thing the Historical Agent talks to. It takes a
current SetupFeatures and an `as_of` timestamp, and returns structured
evidence: overall statistics, breakdowns, and similar-setup matches.

Every read is strictly bounded by `as_of`. The engine never reads the
ledger beyond that wall. This is the core guarantee against future-data
leakage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.models import (
    HistoricalOutcome,
    OutcomeStats,
    SetupFeatures,
    SimilarityMatch,
)
from mt5_platform.historical.similarity import (
    SimilarityScorer,
    matches_from_outcomes,
)
from mt5_platform.historical.statistics import calculate_stats


@dataclass
class HistoricalQuery:
    """A single evidence query against the engine.

    `as_of` is mandatory: it is the temporal wall the engine must not
    cross. The caller is expected to pass the current decision time.
    """

    setup: SetupFeatures
    as_of: datetime
    strategy: str | None = None
    min_similarity: float = 0.5
    top_k: int = 200
    min_strong: int = 100
    min_moderate: int = 30
    min_weak: int = 10


@dataclass
class EvidenceReport:
    """Structured evidence for the Historical Agent.

    The agent reads ONLY this object — never the raw ledger. All fields
    are explicit. Missing samples are explicit, not zero.
    """

    query: HistoricalQuery
    overall: OutcomeStats = field(default_factory=OutcomeStats)
    by_strategy: dict[str, OutcomeStats] = field(default_factory=dict)
    by_regime: dict[str, OutcomeStats] = field(default_factory=dict)
    by_session: dict[str, OutcomeStats] = field(default_factory=dict)
    by_direction: dict[str, OutcomeStats] = field(default_factory=dict)
    similar: list[SimilarityMatch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.query.as_of.isoformat(),
            "instrument": self.query.setup.instrument,
            "overall": self.overall.model_dump(),
            "by_strategy": {k: v.model_dump() for k, v in self.by_strategy.items()},
            "by_regime": {k: v.model_dump() for k, v in self.by_regime.items()},
            "by_session": {k: v.model_dump() for k, v in self.by_session.items()},
            "by_direction": {k: v.model_dump() for k, v in self.by_direction.items()},
            "similar_count": len(self.similar),
            "notes": list(self.notes),
        }


class EvidenceEngine:
    """Stateless evidence engine over a historical ledger.

    The engine owns the ledger and the similarity scorer. It is the
    single point of contact for the Historical Agent.
    """

    def __init__(
        self,
        ledger: InMemoryHistoricalLedger,
        *,
        scorer: SimilarityScorer | None = None,
    ) -> None:
        self.ledger = ledger
        self.scorer = scorer or SimilarityScorer()

    def query(self, q: HistoricalQuery) -> EvidenceReport:
        report = EvidenceReport(query=q)
        setup = q.setup

        # Overall (same instrument, before as_of)
        overall_pool = self.ledger.query(
            instrument=setup.instrument, as_of=q.as_of
        )
        report.overall = calculate_stats(
            overall_pool,
            min_strong=q.min_strong,
            min_moderate=q.min_moderate,
            min_weak=q.min_weak,
        )

        # By strategy
        if q.strategy is not None:
            pool = self.ledger.query(
                instrument=setup.instrument, strategy=q.strategy, as_of=q.as_of
            )
            report.by_strategy[q.strategy] = calculate_stats(
                pool,
                min_strong=q.min_strong,
                min_moderate=q.min_moderate,
                min_weak=q.min_weak,
            )

        # By regime
        if setup.regime is not None:
            pool = self.ledger.query(
                instrument=setup.instrument, regime=setup.regime.value, as_of=q.as_of
            )
            report.by_regime[setup.regime.value] = calculate_stats(
                pool,
                min_strong=q.min_strong,
                min_moderate=q.min_moderate,
                min_weak=q.min_weak,
            )

        # By session
        if setup.session:
            pool = self.ledger.query(
                instrument=setup.instrument, session=setup.session, as_of=q.as_of
            )
            report.by_session[setup.session] = calculate_stats(
                pool,
                min_strong=q.min_strong,
                min_moderate=q.min_moderate,
                min_weak=q.min_weak,
            )

        # By direction
        for direction in ("buy", "sell"):
            pool = self.ledger.query(
                instrument=setup.instrument, direction=direction, as_of=q.as_of
            )
            report.by_direction[direction] = calculate_stats(
                pool,
                min_strong=q.min_strong,
                min_moderate=q.min_moderate,
                min_weak=q.min_weak,
            )

        # Similar setups
        all_pool = self.ledger.query(instrument=setup.instrument, as_of=q.as_of)
        matches = matches_from_outcomes(
            setup, all_pool, scorer=self.scorer, min_similarity=q.min_similarity
        )
        for outcome, sim in matches[: q.top_k]:
            if outcome.features is None:
                continue
            report.similar.append(
                SimilarityMatch(
                    trade_id=outcome.trade_id,
                    similarity=sim,
                    features=outcome.features,
                    outcome=outcome,
                )
            )

        # Notes
        if report.overall.evidence_quality.value == "insufficient":
            report.notes.append(
                f"only {report.overall.sample_size} comparable samples; "
                "evidence is INSUFFICIENT"
            )
        if report.similar and len(report.similar) < 10:
            report.notes.append(
                f"only {len(report.similar)} similar setups above threshold"
            )

        return report
