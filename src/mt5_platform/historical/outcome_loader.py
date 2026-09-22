"""Database -> historical outcome ledger -> EvidenceEngine.

The ledger is the only thing the EvidenceEngine reads. Before this module existed nothing wrote
``HistoricalOutcome`` records at all, so the engine was permanently empty ("insufficient
evidence") no matter how much the bot traded.

Live populations (autonomous + external/manual) load together; backtest outcomes are excluded by
default so forward evidence is never contaminated by replay results. The engine's own thresholds
are passed through unchanged and are never lowered to make evidence appear.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.models import HistoricalOutcome
from mt5_platform.historical.statistics import calculate_stats

LIVE_SOURCES: tuple[str, ...] = ("autonomous", "external")
DEFAULT_MIN_WEAK = 10
DEFAULT_MIN_MODERATE = 30
DEFAULT_MIN_STRONG = 100


async def load_live_outcomes(store: Any, *, limit: int = 5000) -> list[HistoricalOutcome]:
    """Closed live outcomes from the store, oldest first (ledger order == time order)."""
    getter = getattr(store, "get_outcomes", None)
    if not callable(getter):
        return []
    rows: list[HistoricalOutcome] = []
    for source in LIVE_SOURCES:
        rows.extend(await getter(limit=limit, status="closed", source=source))
    rows.sort(key=lambda outcome: outcome.timestamp)
    return rows


def load_ledger(
    outcomes: list[HistoricalOutcome],
    *,
    ledger: InMemoryHistoricalLedger | None = None,
) -> InMemoryHistoricalLedger:
    """Append outcomes to a ledger (creating one when none is supplied)."""
    target = ledger if ledger is not None else InMemoryHistoricalLedger()
    target.record_many(outcomes)
    return target


def evidence_status(
    ledger: InMemoryHistoricalLedger,
    *,
    instrument: str,
    as_of: datetime | None = None,
    min_weak: int = DEFAULT_MIN_WEAK,
    min_moderate: int = DEFAULT_MIN_MODERATE,
    min_strong: int = DEFAULT_MIN_STRONG,
) -> dict[str, Any]:
    """Explicit evidence availability: sample count, quality and the minimum sample required.

    ``thresholds_lowered`` stays False by construction: this function reports the grade the engine
    would give, it never changes it.
    """
    pool = ledger.query(instrument=instrument, as_of=as_of) if instrument else ledger.all()
    stats = calculate_stats(
        pool, min_weak=min_weak, min_moderate=min_moderate, min_strong=min_strong
    )
    sources: dict[str, int] = {}
    for outcome in pool:
        sources[outcome.source.value] = sources.get(outcome.source.value, 0) + 1
    return {
        "instrument": instrument,
        "available_outcomes": len(pool),
        "usable_outcomes": stats.sample_size,
        "open_outcomes": len(pool) - stats.sample_size,
        "sample_size": stats.sample_size,
        "evidence_quality": stats.evidence_quality.value,
        "minimum_sample_required": min_weak,
        "min_sample_weak": min_weak,
        "min_sample_moderate": min_moderate,
        "min_sample_strong": min_strong,
        "sources": sources,
        "backtest_excluded": "backtest" not in sources,
        "thresholds_lowered": False,
        "win_rate": stats.win_rate,
        "expectancy_pct": stats.expectancy,
        "profit_factor": stats.profit_factor,
    }


__all__ = [
    "DEFAULT_MIN_MODERATE",
    "DEFAULT_MIN_STRONG",
    "DEFAULT_MIN_WEAK",
    "LIVE_SOURCES",
    "evidence_status",
    "load_ledger",
    "load_live_outcomes",
]
