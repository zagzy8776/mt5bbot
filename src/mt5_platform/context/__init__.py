"""Market Context Engine (Phase A).

One canonical, measurable, reproducible snapshot of the market for the whole
intelligence system. Agents consume MarketContext; they never re-derive it.
"""

from mt5_platform.context.candles import (
    MultiTimeframeCandleBuilder,
    TimeframeCandleBuilder,
    timeframe_label,
)
from mt5_platform.context.engine import MarketContextEngine
from mt5_platform.context.models import (
    BreakoutState,
    Candle,
    DataQuality,
    LiquidityState,
    MarketContext,
    MomentumFeatures,
    RegimeAssessment,
    SessionInfo,
    StructureFeatures,
    SupportResistanceLevel,
    TrendFeatures,
    VolatilityFeatures,
)
from mt5_platform.context.regime import RegimeClassifier

__all__ = [
    "BreakoutState",
    "Candle",
    "DataQuality",
    "LiquidityState",
    "MarketContext",
    "MarketContextEngine",
    "MomentumFeatures",
    "MultiTimeframeCandleBuilder",
    "RegimeAssessment",
    "RegimeClassifier",
    "SessionInfo",
    "StructureFeatures",
    "SupportResistanceLevel",
    "TimeframeCandleBuilder",
    "TrendFeatures",
    "VolatilityFeatures",
    "timeframe_label",
]
