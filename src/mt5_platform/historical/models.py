"""Historical outcome models.

A HistoricalOutcome is the system's record of one completed (or forcibly
closed) trade. It captures everything needed to reconstruct the decision
context and the realized price path, without depending on live state.

All numeric fields are SI unless noted. Prices are quoted in instrument
units. pnl is in account currency. Time deltas are seconds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from mt5_platform.common.enums import (
    EvidenceQuality,
    OrderSide,
    RegimeLabel,
    TradeCause,
)
from mt5_platform.common.events import utc_now
from mt5_platform.common.ids import new_execution_id, new_order_id


class SetupFeatures(BaseModel):
    """Snapshot of the market at the moment of decision.

    Compact, comparable fingerprint for similarity matching. We deliberately
    avoid embedding raw candles — similarity is computed on these features.
    """

    instrument: str
    timestamp: datetime
    regime: RegimeLabel | None = None
    session: str = ""
    timeframe: str = "M5"
    trend_slope_pct: float | None = None
    trend_efficiency: float | None = None
    volatility_atr: float | None = None
    volatility_atr_to_median: float | None = None
    momentum_roc_pct: float | None = None
    momentum_persistence: float | None = None
    structure_trend: str = "insufficient"  # up | down | range | insufficient
    range_position: float | None = None  # 0..1, where 0=low, 1=high of recent range
    breakout_state: str = "none"  # none | pending | confirmed | failed
    data_quality: str = "ok"  # ok | degraded | critical


class HistoricalOutcome(BaseModel):
    """One recorded trade outcome with the full decision-time context.

    This is the canonical record the evidence engine queries against.
    All fields are immutable once written; corrections create new rows.
    """

    trade_id: str = Field(default_factory=new_execution_id)
    instrument: str
    strategy: str
    direction: OrderSide
    timestamp: datetime  # entry time
    entry: float
    stop_loss: float
    take_profit: float
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: TradeCause = TradeCause.UNKNOWN

    # Snapshot at decision time
    entry_context_id: str = ""
    thesis_id: str = ""
    order_id: str = Field(default_factory=new_order_id)

    # Realized outcome
    realized_pnl: float = 0.0
    return_pct: float = 0.0
    duration_s: float = 0.0

    # Excursions computed from the price path during the trade lifetime
    mae: float = 0.0  # in price units, always >= 0
    mfe: float = 0.0  # in price units, always >= 0
    mae_pct: float = 0.0  # adverse move as % of entry
    mfe_pct: float = 0.0  # favorable move as % of entry

    # Market context at entry (frozen snapshot — no live refs)
    features: SetupFeatures | None = None

    # Agent opinions and thesis that were active at decision time
    # (serialized snapshots — not live objects)
    agent_opinions: list[dict[str, Any]] = Field(default_factory=list)
    thesis_snapshot: dict[str, Any] = Field(default_factory=dict)
    risk_decision: dict[str, Any] = Field(default_factory=dict)

    # Cause classification (may be revised after review)
    cause_class: TradeCause = TradeCause.UNKNOWN

    created_at: datetime = Field(default_factory=utc_now)

    @property
    def is_winner(self) -> bool:
        return self.realized_pnl > 0

    @property
    def is_loser(self) -> bool:
        return self.realized_pnl < 0

    @property
    def is_closed(self) -> bool:
        return self.exit_price is not None


class OutcomeStats(BaseModel):
    """Aggregated statistics over a set of HistoricalOutcome records.

    Every breakdown must carry its own sample_size and evidence_quality.
    Statistics with insufficient samples must be explicitly marked.
    """

    sample_size: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    loss_rate: float = 0.0
    expectancy: float = 0.0  # average return per trade
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    avg_mae_pct: float = 0.0
    avg_mfe_pct: float = 0.0
    max_losing_streak: int = 0
    outcome_distribution: dict[str, int] = Field(default_factory=dict)
    evidence_quality: EvidenceQuality = EvidenceQuality.INSUFFICIENT
    min_sample_strong: int = 100
    min_sample_moderate: int = 30
    min_sample_weak: int = 10


class SimilarityMatch(BaseModel):
    """One historical outcome that matched the query, with its score."""

    trade_id: str
    similarity: float = Field(ge=0.0, le=1.0)
    features: SetupFeatures
    outcome: HistoricalOutcome
