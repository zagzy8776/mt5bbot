"""Live trade recording and the historical outcome ledger.

The recorder observes broker truth and freezes one HistoricalOutcome per position. It is passive:
it never blocks, delays or alters an order, and a persistence failure is retried instead of
failing the trading path.
"""

from mt5_platform.outcomes.excursions import ExcursionTracker
from mt5_platform.outcomes.exit_cause import (
    cause_from_levels,
    cause_from_position_decision,
    level_tolerance,
)
from mt5_platform.outcomes.learning import OutcomeLearningPipeline, to_trade_outcome
from mt5_platform.outcomes.recorder import (
    OutcomeRecorderStats,
    TradeOutcomeRecorder,
    build_setup_features,
    source_for_position,
    trade_id_for_ticket,
)

__all__ = [
    "ExcursionTracker",
    "OutcomeLearningPipeline",
    "OutcomeRecorderStats",
    "TradeOutcomeRecorder",
    "build_setup_features",
    "cause_from_levels",
    "cause_from_position_decision",
    "level_tolerance",
    "source_for_position",
    "to_trade_outcome",
    "trade_id_for_ticket",
]
