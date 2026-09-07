"""Canonical MarketContext models (Phase A).

Every value is measurable and reproducible from the tick/candle stream.
Agents consume this object — they never re-derive market state themselves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from mt5_platform.common.enums import DataQualityLevel, RegimeLabel
from mt5_platform.common.ids import new_context_id


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    tick_count: int = 0


class TrendFeatures(BaseModel):
    slope_per_bar_pct: float | None = None
    efficiency_ratio: float | None = None  # Kaufman ER in [0, 1]
    structure_score: float | None = None  # [-1, 1]: +1 pure HH/HL, -1 pure LH/LL
    net_change_pct: float | None = None


class VolatilityFeatures(BaseModel):
    atr: float | None = None
    atr_percentile: float | None = None  # 0..100 vs trailing ATR series
    atr_to_median: float | None = None
    range_pct: float | None = None  # window (high-low)/price * 100


class MomentumFeatures(BaseModel):
    roc_pct: float | None = None
    persistence: float | None = None  # 0..1 share of bars agreeing with net move
    consecutive_same_dir: int = 0


class StructureFeatures(BaseModel):
    swing_highs: int = 0
    swing_lows: int = 0
    higher_highs: int = 0
    lower_highs: int = 0
    higher_lows: int = 0
    lower_lows: int = 0
    structure_trend: str = "insufficient"  # up | down | range | insufficient


class SupportResistanceLevel(BaseModel):
    price: float
    kind: str  # support | resistance
    touches: int
    distance_pct: float  # signed % from current price (+ above, - below)


class BreakoutState(BaseModel):
    state: str = "none"  # none | pending | confirmed | failed
    direction: str | None = None  # up | down
    boundary: float | None = None  # prior range boundary that was tested
    bars_outside: int = 0
    retrace_pct: float | None = None  # pullback from breakout extreme, 0..100


class LiquidityState(BaseModel):
    current_spread: float | None = None
    spread_median: float | None = None
    spread_percentile: float | None = None
    level: str | None = None  # normal | wide | extreme | insufficient


class SessionInfo(BaseModel):
    label: str  # asia | london | london_ny_overlap | new_york | weekend
    utc_hour: int
    weekday: str


class DataQuality(BaseModel):
    level: DataQualityLevel = DataQualityLevel.OK
    last_tick_age_s: float | None = None
    tick_count: int = 0
    duplicate_count: int = 0
    malformed_count: int = 0
    inconsistent_count: int = 0
    missing_bars: int = 0
    missing_bar_ratio: float = 0.0
    insufficient_history_timeframes: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class RegimeAssessment(BaseModel):
    regime: RegimeLabel
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    classified_at: datetime


class MarketContext(BaseModel):
    """One canonical snapshot of everything the intelligence system sees.

    context_id is the decision-memory key: "what exactly did the system see
    when this trade was considered?"
    """

    context_id: str = Field(default_factory=new_context_id)
    instrument: str
    timestamp: datetime  # last accepted tick time (UTC)
    bid: float | None = None
    ask: float | None = None
    current_price: float  # mid of bid/ask, or last price
    session: SessionInfo
    candles: dict[str, list[Candle]] = Field(default_factory=dict)
    trend: TrendFeatures | None = None
    volatility: VolatilityFeatures | None = None
    momentum: MomentumFeatures | None = None
    structure: StructureFeatures | None = None
    support_resistance: list[SupportResistanceLevel] = Field(default_factory=list)
    breakout: BreakoutState = Field(default_factory=BreakoutState)
    liquidity: LiquidityState = Field(default_factory=LiquidityState)
    regime: RegimeLabel | None = None
    regime_confidence: float | None = None
    regime_evidence: dict[str, Any] = Field(default_factory=dict)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    usable_for_trading: bool = False