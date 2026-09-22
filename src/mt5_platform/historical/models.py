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
    ExitCause,
    OrderSide,
    OutcomeSource,
    OutcomeStatus,
    RegimeLabel,
    TradeCause,
)
from mt5_platform.common.events import utc_now
from mt5_platform.common.ids import new_execution_id, new_order_id


class SetupFeatures(BaseModel):
    """Snapshot of the market at the moment of decision.

    Compact, comparable fingerprint for similarity matching. We deliberately
    avoid embedding raw candles — similarity is computed on these features.

    Immutable after the trade opens: the recorder never rewrites entry knowledge.
    Fields stay ``None``/"unavailable" when the runtime did not measure them.
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
    # Entry-condition detail available at decision time (no future information).
    spread_points: float | None = None
    spread_percentile: float | None = None
    liquidity_level: str = ""  # normal | wide | extreme | insufficient | ""
    strategy: str = ""
    strategy_version: str = ""
    signal_confidence: float | None = None
    signal_metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_quality: str = ""  # insufficient | weak | moderate | strong | ""
    risk_state: dict[str, Any] = Field(default_factory=dict)
    thesis_confidence: float | None = None


OUTCOME_SCHEMA_VERSION = "1.0"


class TradeLeg(BaseModel):
    """One execution leg of a trade: the entry, a partial exit, or the final exit.

    Partial closes are legs, never a separate trade: the parent outcome is finalized only when
    the remaining volume reaches zero, so no information is lost and no duplicate record appears.
    """

    leg_id: str = Field(default_factory=new_execution_id)
    trade_id: str
    kind: str  # entry | partial_exit | exit
    timestamp: datetime
    price: float | None = None
    volume: float = 0.0
    realized_pnl: float | None = None
    commission: float | None = None
    swap: float | None = None
    slippage: float | None = None
    exit_cause: ExitCause | None = None
    reason: str = ""
    broker_deal: str | None = None


class HistoricalOutcome(BaseModel):
    """One recorded trade outcome with the full decision-time context.

    This is the canonical record the evidence engine queries against. Entry-time knowledge
    (``features``, ``thesis_snapshot``, ``agent_opinions``, ``risk_decision``) is written once and
    never rewritten. Only outcome fields (exit, excursions, realized money, legs) evolve until the
    position is fully closed, after which the record is frozen.
    """

    schema_version: str = OUTCOME_SCHEMA_VERSION
    status: OutcomeStatus = OutcomeStatus.CLOSED
    trade_id: str = Field(default_factory=new_execution_id)
    instrument: str
    timeframe: str = ""
    strategy: str = ""
    strategy_version: str = ""
    direction: OrderSide
    source: OutcomeSource = OutcomeSource.AUTONOMOUS

    # Broker identity (None when the broker/runtime did not provide it)
    broker_ticket: str | None = None
    position_id: str | None = None
    order_id: str = Field(default_factory=new_order_id)

    timestamp: datetime  # entry time
    entry: float
    stop_loss: float | None = None  # initial protective stop at decision time
    take_profit: float | None = None  # initial target at decision time
    entry_volume: float | None = None
    remaining_volume: float | None = None
    entry_commission: float | None = None
    entry_slippage: float | None = None

    # Levels as they stand after trailing / break-even / target changes
    final_stop_loss: float | None = None
    final_take_profit: float | None = None

    # Exit
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: TradeCause = TradeCause.UNKNOWN
    exit_cause: ExitCause = ExitCause.UNKNOWN
    exit_cause_source: str = ""  # runtime | decision_reason | level_match | external | backtest

    # Snapshot at decision time
    entry_context_id: str = ""
    thesis_id: str = ""

    # Realized outcome
    realized_pnl: float = 0.0
    return_pct: float = 0.0
    realized_pnl_pct: float | None = None
    duration_s: float = 0.0
    r_multiple: float | None = None
    commission: float | None = None
    swap: float | None = None
    slippage: float | None = None

    # Excursions from the observed price path during the trade lifetime
    mae: float = 0.0  # in price units, always >= 0
    mfe: float = 0.0  # in price units, always >= 0
    mae_pct: float = 0.0  # adverse move as % of entry
    mfe_pct: float = 0.0  # favorable move as % of entry
    excursion_samples: int = 0

    # Market context at entry (frozen snapshot — no live refs)
    features: SetupFeatures | None = None
    regime_snapshot: dict[str, Any] = Field(default_factory=dict)

    # Agent opinions and thesis that were active at decision time
    # (serialized snapshots — not live objects)
    agent_opinions: list[dict[str, Any]] = Field(default_factory=list)
    thesis_snapshot: dict[str, Any] = Field(default_factory=dict)
    risk_decision: dict[str, Any] = Field(default_factory=dict)

    # Confidence / evidence information captured at entry and at close
    evidence: dict[str, Any] = Field(default_factory=dict)

    # Partial exits and the final exit, in chronological order
    legs: list[TradeLeg] = Field(default_factory=list)

    # Cause classification (may be revised after review)
    cause_class: TradeCause = TradeCause.UNKNOWN

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def symbol(self) -> str:
        return self.instrument

    @property
    def side(self) -> str:
        return self.direction.value

    @property
    def volume(self) -> float | None:
        return self.entry_volume

    @property
    def initial_stop_loss(self) -> float | None:
        return self.stop_loss or None

    @property
    def initial_take_profit(self) -> float | None:
        return self.take_profit or None

    @property
    def holding_time_s(self) -> float:
        return self.duration_s

    @property
    def is_open(self) -> bool:
        return self.status is OutcomeStatus.OPEN

    def stop_distance(self) -> float | None:
        """Initial risk per unit, or None when the stop was missing/unusable."""
        if self.stop_loss is None or self.entry is None:
            return None
        distance = abs(self.entry - self.stop_loss)
        return distance if distance > 0 else None

    def compute_r_multiple(self) -> float | None:
        """Realized R: the outcome normalized by the ORIGINAL stop distance."""
        distance = self.stop_distance()
        if distance is None or self.exit_price is None:
            return None
        direction = 1.0 if self.direction is OrderSide.BUY else -1.0
        return direction * (self.exit_price - self.entry) / distance

    @property
    def is_winner(self) -> bool:
        return self.realized_pnl > 0

    @property
    def is_loser(self) -> bool:
        return self.realized_pnl < 0

    @property
    def is_closed(self) -> bool:
        return self.status is OutcomeStatus.CLOSED and self.exit_price is not None


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
