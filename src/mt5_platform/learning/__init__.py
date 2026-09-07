"""Learning, post-trade review and decision memory (Phase E).

This package records what the system believed, what actually happened,
and what lessons were learned — without allowing uncontrolled
self-modification.

Key principle: facts are immutable. Interpretations are hypotheses.
A hypothesis must be validated before it can influence configuration.
"""

from mt5_platform.learning.calibration import ConfidenceCalibrationTracker
from mt5_platform.learning.hypothesis import (
    HypothesisRegistry,
)
from mt5_platform.learning.memory import (
    DecisionMemory,
    MemoryQuery,
)
from mt5_platform.learning.models import (
    AgentPerformanceRecord,
    ConfigurationVersion,
    Counterfactual,
    DecisionMemoryRecord,
    Lesson,
    LessonEvidence,
    PostTradeReview,
    ReviewOutcome,
    TradeOutcome,
)
from mt5_platform.learning.performance import AgentPerformanceTracker
from mt5_platform.learning.review import PostTradeReviewEngine
from mt5_platform.learning.versioning import (
    ConfigurationVersionStore,
)

__all__ = [
    "AgentPerformanceRecord",
    "AgentPerformanceTracker",
    "ConfidenceCalibrationTracker",
    "ConfigurationProposal",
    "ConfigurationVersion",
    "ConfigurationVersionStore",
    "Counterfactual",
    "DecisionMemory",
    "DecisionMemoryRecord",
    "HypothesisRegistry",
    "Lesson",
    "LessonEvidence",
    "MemoryQuery",
    "PostTradeReview",
    "PostTradeReviewEngine",
    "ReviewOutcome",
    "TradeOutcome",
]
