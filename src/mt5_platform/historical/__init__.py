"""Historical Evidence Engine (Phase C).

A measured-evidence layer that records the system's actual trade outcomes
and provides statistically honest comparisons to the current MarketContext.

This is NOT a price-prediction model. It is an audit-grade evidence ledger
that lets the Historical Agent answer one question:

"Have we seen setups like this before, and how did they actually resolve?"

Every output is explicitly marked with sample size and evidence quality.
No probability is ever fabricated from a small sample.
"""

from mt5_platform.historical.excursion import compute_mae_mfe
from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.models import (
    EvidenceQuality,
    HistoricalOutcome,
    OutcomeStats,
    SetupFeatures,
    SimilarityMatch,
    TradeCause,
)
from mt5_platform.historical.similarity import (
    FeatureSimilarity,
    SimilarityScorer,
)
from mt5_platform.historical.statistics import (
    calculate_stats,
    calculate_streak,
)
from mt5_platform.historical.engine import EvidenceEngine, HistoricalQuery

__all__ = [
    "EvidenceEngine",
    "EvidenceQuality",
    "FeatureSimilarity",
    "HistoricalOutcome",
    "HistoricalQuery",
    "InMemoryHistoricalLedger",
    "OutcomeStats",
    "SetupFeatures",
    "SimilarityMatch",
    "SimilarityScorer",
    "TradeCause",
    "calculate_stats",
    "calculate_streak",
    "compute_mae_mfe",
]
