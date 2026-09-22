"""Live trade recorder: turns broker positions into frozen HistoricalOutcome records.

Design rules (each one protects the trading path):

* **Passive.** Recording can never block, alter or delay an order: every store interaction is
  wrapped, failures queue for retry, and nothing in this module raises into the runtime.
* **One record per broker position**, keyed by ticket, with a deterministic trade id, so
  reconciliation seeing the same position twice can never create a duplicate.
* **Entry knowledge is written once.** Only outcome fields (exit, excursions, money, legs) evolve
  until the position is fully flat, after which the record is frozen.
* **Nothing is invented.** Unknown exits, absent commissions, missing prices and unavailable
  profit stay explicitly unknown/None, with a ``*_source`` marker saying how they were obtained.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import (
    AuditEventType,
    ExitCause,
    OutcomeSource,
    OutcomeStatus,
    Severity,
    exit_cause_to_trade_cause,
)
from mt5_platform.common.events import AuditEvent, PositionInfo, utc_now
from mt5_platform.historical.features import compute_candle_shape
from mt5_platform.historical.models import HistoricalOutcome, SetupFeatures, TradeLeg
from mt5_platform.outcomes.excursions import ExcursionTracker
from mt5_platform.outcomes.exit_cause import cause_from_levels, level_tolerance

VOLUME_EPSILON = 1e-9
_TRADE_ID_PREFIX = "trd_"


@dataclass
class OutcomeRecorderStats:
    """Counters for the dashboard: what the recorder saw, did, and failed to persist."""

    observed_positions: int = 0
    opened: int = 0
    updated: int = 0
    closed: int = 0
    partial_exits: int = 0
    modifications: int = 0
    duplicates_ignored: int = 0
    orphans_finalized: int = 0
    recovered_open: int = 0
    persist_attempts: int = 0
    persist_ok: int = 0
    persist_failures: int = 0
    persist_retried: int = 0
    pending_persist: int = 0
    storage_available: bool = True
    last_error: str | None = None
    last_persist_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def trade_id_for_ticket(ticket: str) -> str:
    """Deterministic trade id: the same broker ticket always maps to the same record."""
    return f"{_TRADE_ID_PREFIX}{ticket}"


def source_for_position(position: PositionInfo) -> OutcomeSource:
    """Autonomous = this bot opened it (its own magic); anything else is external/manual."""
    return OutcomeSource.EXTERNAL if position.is_external else OutcomeSource.AUTONOMOUS


def _pick(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present attribute/key. Accepts objects and plain dicts alike."""
    if obj is None:
        return default
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return default


def build_setup_features(
    *,
    symbol: str,
    when: datetime,
    timeframe: str = "",
    strategy: str = "",
    strategy_version: str = "",
    signal: Any | None = None,
    context: Any | None = None,
    risk_decision: Any | None = None,
    evidence: dict[str, Any] | None = None,
    spread_points: float | None = None,
    candles: Sequence[Any] | None = None,
) -> SetupFeatures:
    """Freeze what the system knew at entry, using only measurements that exist."""
    shape = compute_candle_shape(candles) if candles else None
    trend = _pick(context, "trend")
    volatility = _pick(context, "volatility")
    momentum = _pick(context, "momentum")
    structure = _pick(context, "structure")
    liquidity = _pick(context, "liquidity")
    breakout = _pick(context, "breakout")
    session = _pick(context, "session")
    features = SetupFeatures(
        instrument=symbol,
        timestamp=when,
        timeframe=timeframe or "M5",
        regime=_pick(context, "regime"),
        session=str(_pick(session, "label", default="")),
        trend_slope_pct=_pick(trend, "slope_per_bar_pct"),
        trend_efficiency=_pick(trend, "efficiency_ratio"),
        volatility_atr=_pick(volatility, "atr"),
        volatility_atr_to_median=_pick(volatility, "atr_to_median"),
        momentum_roc_pct=_pick(momentum, "roc_pct"),
        momentum_persistence=_pick(momentum, "persistence"),
        structure_trend=str(_pick(structure, "structure_trend", default="insufficient")),
        breakout_state=str(_pick(breakout, "state", default="none")),
        data_quality=str(_pick(_pick(context, "data_quality"), "level", default="")),
        spread_points=(
            spread_points if spread_points is not None else _pick(liquidity, "current_spread")
        ),
        spread_percentile=_pick(liquidity, "spread_percentile"),
        liquidity_level=str(_pick(liquidity, "level", default="")),
        strategy=strategy,
        strategy_version=strategy_version,
        signal_confidence=_pick(signal, "confidence"),
        signal_metadata=dict(_pick(signal, "metadata", default={}) or {}),
        evidence_quality=str((evidence or {}).get("evidence_quality", "")),
        risk_state={
            "approved": bool(_pick(risk_decision, "approved", default=False)),
            "reasons": list(_pick(risk_decision, "reasons", default=[]) or []),
        },
        thesis_confidence=float((evidence or {}).get("thesis_confidence", 0.0) or 0.0) or None,
        candle_features=shape.to_dict() if shape is not None else {},
    )
    return features


class TradeOutcomeRecorder:
    """Passive recorder: observes broker positions, tracks excursions, freezes final outcomes."""

    def __init__(
        self,
        store: Any | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        on_completed: Callable[[HistoricalOutcome], Any] | None = None,
        max_pending: int = 1000,
    ) -> None:
        self.store = store
        self._clock = clock
        self._on_completed = on_completed
        self._max_pending = max_pending
        self._records: dict[str, HistoricalOutcome] = {}
        self._trackers: dict[str, ExcursionTracker] = {}
        self._tick_sizes: dict[str, float | None] = {}
        self._closed: set[str] = set()
        self._pending: deque[str] = deque()
        self._resumed: set[str] = set()
        self.stats = OutcomeRecorderStats()
        self._storage_checked = False

    # ------------------------------------------------------------------ internals

    @staticmethod
    def key_for(position: PositionInfo | str) -> str:
        """Position key: the broker ticket (the only stable broker-side identity)."""
        return position if isinstance(position, str) else str(position.ticket)

    def _storage(self) -> Any | None:
        writer = getattr(self.store, "write_outcome", None)
        if callable(writer):
            self.stats.storage_available = True
            return writer
        if not self._storage_checked:
            self._storage_checked = True
            self.stats.storage_available = False
            self._emit(
                AuditEventType.SINK_FAILED,
                Severity.WARNING,
                {"stage": "outcome_storage", "reason": "store does not implement write_outcome"},
            )
        return None

    def _emit(
        self,
        event_type: AuditEventType,
        severity: Severity,
        payload: dict[str, Any],
        *,
        symbol: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        audit_log.emit(
            AuditEvent(
                component="outcome_recorder",
                event_type=event_type.value,
                severity=severity,
                symbol=symbol,
                correlation_id=correlation_id or "",
                payload=payload,
            )
        )

    def _touch(self, key: str) -> None:
        """Copy live excursion state onto the record and queue it for persistence."""
        record = self._records.get(key)
        if record is None:
            return
        tracker = self._trackers.get(key)
        if tracker is not None:
            record.mae = tracker.mae
            record.mfe = tracker.mfe
            record.mae_pct = tracker.mae_pct()
            record.mfe_pct = tracker.mfe_pct()
            record.excursion_samples = tracker.samples
            if tracker.last_price is not None:
                record.evidence["last_observed_price"] = tracker.last_price
        record.updated_at = self._clock()
        if key not in self._pending:
            if len(self._pending) < self._max_pending:
                self._pending.append(key)
            else:
                self.stats.last_error = "outcome persistence queue is full"
        self.stats.pending_persist = len(self._pending)

    def _tracker_for(self, record: HistoricalOutcome) -> ExcursionTracker:
        key = record.broker_ticket or record.trade_id
        tracker = self._trackers.get(key)
        if tracker is None:
            tracker = ExcursionTracker(side=record.direction, entry=record.entry)
            self._trackers[key] = tracker
        return tracker

    # ------------------------------------------------------------- observation

    def observe_position(
        self,
        position: PositionInfo,
        *,
        context: Any | None = None,
        signal: Any | None = None,
        setup: SetupFeatures | None = None,
        thesis: dict[str, Any] | None = None,
        agent_opinions: list[dict[str, Any]] | None = None,
        risk_decision: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        strategy: str = "",
        strategy_version: str = "",
        timeframe: str = "",
        tick_size: float | None = None,
        spread_points: float | None = None,
        entry_context_id: str = "",
        thesis_id: str = "",
        order_id: str = "",
        candles: Sequence[Any] | None = None,
    ) -> HistoricalOutcome | None:
        """Create the record on first sighting, then keep it current.

        Never re-opens a finalized ticket, never invents a missing price, never raises.
        """
        if position is None or getattr(position, "ticket", None) is None:
            return None
        key = self.key_for(position)
        self.stats.observed_positions += 1
        if tick_size is not None:
            self._tick_sizes[key] = float(tick_size)

        record = self._records.get(key)
        if record is None:
            if key in self._closed:
                self.stats.duplicates_ignored += 1
                return None
            return self._open_record(
                position,
                key=key,
                context=context,
                signal=signal,
                setup=setup,
                thesis=thesis,
                agent_opinions=agent_opinions,
                risk_decision=risk_decision,
                evidence=evidence,
                strategy=strategy,
                strategy_version=strategy_version,
                timeframe=timeframe,
                spread_points=spread_points,
                entry_context_id=entry_context_id,
                thesis_id=thesis_id,
                order_id=order_id,
                candles=candles,
            )

        if record.status is OutcomeStatus.CLOSED:
            self.stats.duplicates_ignored += 1
            return record
        self._tracker_for(record).observe(position.current_price)
        self._refresh_from_position(record, position)
        self.stats.updated += 1
        self._touch(key)
        return record

    def _refresh_from_position(self, record: HistoricalOutcome, position: PositionInfo) -> None:
        """Fold broker truth into the record: reductions and level changes we did not initiate."""
        key = record.broker_ticket or record.trade_id
        if position.volume is not None:
            current = float(position.volume)
            remaining = (
                record.remaining_volume
                if record.remaining_volume is not None
                else record.entry_volume
            )
            if remaining is not None and current < remaining - VOLUME_EPSILON:
                self.record_partial_close(
                    key,
                    volume=remaining - current,
                    price=position.current_price,
                    observed=True,
                )
            record.remaining_volume = current
        new_stop = float(position.stop_loss) if position.stop_loss else None
        new_target = float(position.take_profit) if position.take_profit else None
        stored_stop = (
            record.final_stop_loss if record.final_stop_loss is not None else record.stop_loss
        )
        stored_target = (
            record.final_take_profit if record.final_take_profit is not None else record.take_profit
        )
        if new_stop != stored_stop or new_target != stored_target:
            self.record_modification(
                key,
                stop_loss=new_stop,
                take_profit=new_target,
                reason="observed_at_broker",
            )

    def _open_record(
        self,
        position: PositionInfo,
        *,
        key: str,
        context: Any | None,
        signal: Any | None,
        setup: SetupFeatures | None,
        thesis: dict[str, Any] | None,
        agent_opinions: list[dict[str, Any]] | None,
        risk_decision: dict[str, Any] | None,
        evidence: dict[str, Any] | None,
        strategy: str,
        strategy_version: str,
        timeframe: str,
        spread_points: float | None,
        entry_context_id: str,
        thesis_id: str,
        order_id: str,
        candles: Sequence[Any] | None = None,
    ) -> HistoricalOutcome:
        """Write entry knowledge exactly once; only outcome fields evolve afterwards."""
        when = position.opened_at or self._clock()
        entry = float(position.entry_price)
        volume = float(position.volume) if position.volume is not None else None
        stop = float(position.stop_loss) if position.stop_loss else None
        target = float(position.take_profit) if position.take_profit else None
        trade_id = trade_id_for_ticket(key)
        features = setup or build_setup_features(
            symbol=position.symbol,
            when=when,
            timeframe=timeframe,
            strategy=strategy,
            strategy_version=strategy_version,
            signal=signal,
            context=context,
            risk_decision=risk_decision,
            evidence=evidence,
            spread_points=spread_points,
            candles=candles,
        )
        extra: dict[str, Any] = {"order_id": order_id} if order_id else {}
        record = HistoricalOutcome(
            status=OutcomeStatus.OPEN,
            trade_id=trade_id,
            instrument=position.symbol,
            timeframe=timeframe,
            strategy=strategy,
            strategy_version=strategy_version,
            direction=position.side,
            source=source_for_position(position),
            broker_ticket=key,
            position_id=key,
            timestamp=when,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            entry_volume=volume,
            remaining_volume=volume,
            final_stop_loss=stop,
            final_take_profit=target,
            features=features,
            thesis_snapshot=dict(thesis or {}),
            agent_opinions=list(agent_opinions or []),
            risk_decision=dict(risk_decision or {}),
            evidence={
                **(evidence or {}),
                "realized_pnl_source": "unavailable",
                "tick_size": self._tick_sizes.get(key),
            },
            entry_context_id=entry_context_id,
            thesis_id=thesis_id,
            regime_snapshot={
                "regime": str(_pick(context, "regime", default="") or ""),
                "source": "market_context" if context is not None else "unavailable",
            },
            legs=[
                TradeLeg(
                    trade_id=trade_id,
                    kind="entry",
                    timestamp=when,
                    price=entry,
                    volume=volume or 0.0,
                    reason="position observed open",
                )
            ],
            **extra,
        )
        self._records[key] = record
        tracker = ExcursionTracker(side=position.side, entry=entry)
        tracker.observe(position.current_price)
        self._trackers[key] = tracker
        self.stats.opened += 1
        self._touch(key)
        self._emit(
            AuditEventType.POSITION_OPENED,
            Severity.INFO,
            {
                "stage": "outcome_opened",
                "trade_id": trade_id,
                "ticket": key,
                "source": record.source.value,
                "strategy": strategy,
            },
            symbol=position.symbol,
            correlation_id=key,
        )
        return record

    def observe_price(
        self, key: str, price: float | None, *, high: float | None = None, low: float | None = None
    ) -> None:
        """Track excursions between reconciliations. Persists only when an extreme moves."""
        record = self._records.get(key)
        tracker = self._trackers.get(key)
        if record is None or tracker is None or record.status is OutcomeStatus.CLOSED:
            return
        before = (tracker.mae, tracker.mfe)
        if high is not None or low is not None:
            tracker.observe_high_low(
                high if high is not None else price, low if low is not None else price
            )
        else:
            tracker.observe(price)
        if (tracker.mae, tracker.mfe) != before:
            self._touch(key)

    def record_modification(
        self,
        key: str,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        when: datetime | None = None,
        reason: str = "",
    ) -> HistoricalOutcome | None:
        """Record a protection-level change (trailing stop, break-even, new target)."""
        record = self._records.get(key)
        if record is None or record.status is OutcomeStatus.CLOSED:
            return None
        if stop_loss is not None:
            record.final_stop_loss = float(stop_loss)
        if take_profit is not None:
            record.final_take_profit = float(take_profit)
        history = list(record.evidence.get("modifications", []))
        history.append(
            {
                "at": (when or self._clock()).isoformat(),
                "stop_loss": record.final_stop_loss,
                "take_profit": record.final_take_profit,
                "reason": reason,
            }
        )
        record.evidence["modifications"] = history[-20:]
        self.stats.modifications += 1
        self._touch(key)
        self._emit(
            AuditEventType.POSITION_MODIFIED,
            Severity.INFO,
            {
                "stage": "outcome_modified",
                "trade_id": record.trade_id,
                "stop_loss": record.final_stop_loss,
                "take_profit": record.final_take_profit,
                "reason": reason,
            },
            symbol=record.instrument,
            correlation_id=key,
        )
        return record

    def record_partial_close(
        self,
        key: str,
        *,
        volume: float,
        price: float | None = None,
        realized_pnl: float | None = None,
        commission: float | None = None,
        swap: float | None = None,
        slippage: float | None = None,
        cause: ExitCause = ExitCause.PARTIAL_EXIT,
        when: datetime | None = None,
        reason: str = "",
        observed: bool = False,
        broker_deal: str | None = None,
    ) -> TradeLeg | None:
        """Record a partial exit as a leg. The parent outcome is never finalized here."""
        record = self._records.get(key)
        if record is None or record.status is OutcomeStatus.CLOSED:
            return None
        try:
            closed_volume = float(volume)
        except (TypeError, ValueError):
            return None
        if closed_volume <= 0:
            return None
        remaining_before = (
            record.remaining_volume
            if record.remaining_volume is not None
            else record.entry_volume
        )
        if remaining_before is not None:
            closed_volume = min(closed_volume, remaining_before)
        if closed_volume <= VOLUME_EPSILON:
            return None  # nothing left to reduce: do not record an empty leg
        leg = TradeLeg(
            trade_id=record.trade_id,
            kind="partial_exit",
            timestamp=when or self._clock(),
            price=float(price) if price is not None else None,
            volume=closed_volume,
            realized_pnl=realized_pnl,
            commission=commission,
            swap=swap,
            slippage=slippage,
            exit_cause=cause,
            reason=reason or ("observed_volume_delta" if observed else "partial_exit"),
            broker_deal=broker_deal,
        )
        record.legs.append(leg)
        if remaining_before is not None:
            record.remaining_volume = max(0.0, remaining_before - closed_volume)
        self._add_cost(record, commission=commission, swap=swap, slippage=slippage)
        if realized_pnl is not None:
            record.realized_pnl = float(record.realized_pnl or 0.0) + float(realized_pnl)
            record.evidence["realized_pnl_source"] = "legs"
        self.stats.partial_exits += 1
        self._touch(key)
        self._emit(
            AuditEventType.POSITION_REDUCED,
            Severity.INFO,
            {
                "stage": "outcome_partial_exit",
                "trade_id": record.trade_id,
                "volume": closed_volume,
                "remaining_volume": record.remaining_volume,
                "cause": cause.value,
                "observed": observed,
            },
            symbol=record.instrument,
            correlation_id=key,
        )
        return leg

    @staticmethod
    def _add_cost(
        record: HistoricalOutcome,
        *,
        commission: float | None = None,
        swap: float | None = None,
        slippage: float | None = None,
    ) -> None:
        """Accumulate broker costs, without inventing values that were never reported."""
        for name, value in (
            ("commission", commission),
            ("swap", swap),
            ("slippage", slippage),
        ):
            if value is None:
                continue
            current = getattr(record, name)
            setattr(record, name, float(value) + (float(current) if current is not None else 0.0))

    def finalize(
        self,
        key: str,
        *,
        exit_price: float | None = None,
        cause: ExitCause | None = None,
        cause_source: str | None = None,
        realized_pnl: float | None = None,
        commission: float | None = None,
        swap: float | None = None,
        slippage: float | None = None,
        when: datetime | None = None,
        reason: str = "",
        broker_deal: str | None = None,
    ) -> HistoricalOutcome | None:
        """Freeze the record once the position is flat. Idempotent: a repeat call is a no-op.

        ``realized_pnl`` is the profit of THIS exit (deal-level); legs are summed, so partial exits
        and the final close add up without double counting. Nothing is invented: with no reported
        money the record says ``realized_pnl_source = unavailable``.
        """
        record = self._records.get(key)
        if record is None:
            return None  # never invent an outcome for a position we never observed
        if record.status is OutcomeStatus.CLOSED:
            self.stats.duplicates_ignored += 1
            return record

        closed_at = when or self._clock()
        tracker = self._trackers.get(key)
        price = (
            float(exit_price)
            if exit_price is not None
            else (tracker.last_price if tracker else None)
        )
        if tracker is not None:
            tracker.observe(price)

        # Exit cause: the component that exited wins; otherwise match the recorded levels only.
        if cause is not None and cause is not ExitCause.UNKNOWN:
            final_cause, final_source = cause, (cause_source or "runtime")
        else:
            matched, matched_source = cause_from_levels(
                exit_price=price,
                stop_loss=record.final_stop_loss or record.stop_loss,
                take_profit=record.final_take_profit or record.take_profit,
                tolerance=level_tolerance(self._tick_sizes.get(key)),
            )
            final_cause = matched
            final_source = matched_source if matched is not ExitCause.UNKNOWN else (
                cause_source or matched_source
            )

        exit_volume = record.remaining_volume
        if exit_volume is None:
            exit_volume = record.entry_volume
        record.legs.append(
            TradeLeg(
                trade_id=record.trade_id,
                kind="exit",
                timestamp=closed_at,
                price=price,
                volume=float(exit_volume or 0.0),
                realized_pnl=realized_pnl,
                commission=commission,
                swap=swap,
                slippage=slippage,
                exit_cause=final_cause,
                reason=reason or "position closed",
                broker_deal=broker_deal,
            )
        )
        self._add_cost(record, commission=commission, swap=swap, slippage=slippage)

        reported = [leg.realized_pnl for leg in record.legs if leg.realized_pnl is not None]
        if reported:
            record.realized_pnl = float(sum(reported))
            record.evidence["realized_pnl_source"] = "legs"
        elif realized_pnl is not None:
            record.realized_pnl = float(realized_pnl)
            record.evidence["realized_pnl_source"] = "reported"
        else:
            record.realized_pnl = 0.0
            record.evidence["realized_pnl_source"] = "unavailable"
        if record.evidence.get("realized_pnl_source") != "unavailable" and record.entry:
            record.return_pct = record.realized_pnl / record.entry * 100.0
            record.realized_pnl_pct = record.return_pct

        record.exit_price = price
        record.exit_time = closed_at
        record.remaining_volume = 0.0
        record.exit_cause = final_cause
        record.exit_cause_source = final_source
        record.cause_class = exit_cause_to_trade_cause(final_cause)
        record.exit_reason = record.cause_class
        record.r_multiple = record.compute_r_multiple()
        try:
            record.duration_s = max(0.0, (closed_at - record.timestamp).total_seconds())
        except TypeError:  # naive/aware mix from a caller: keep the previous value
            pass
        record.status = OutcomeStatus.CLOSED
        self._closed.add(key)
        self.stats.closed += 1
        self._touch(key)
        self._emit(
            AuditEventType.POSITION_CLOSED,
            Severity.INFO,
            {
                "stage": "outcome_closed",
                "trade_id": record.trade_id,
                "ticket": key,
                "source": record.source.value,
                "strategy": record.strategy,
                "exit_cause": final_cause.value,
                "exit_cause_source": final_source,
                "realized_pnl": record.realized_pnl,
                "realized_pnl_source": record.evidence.get("realized_pnl_source"),
                "r_multiple": record.r_multiple,
                "mae": record.mae,
                "mfe": record.mfe,
                "duration_s": record.duration_s,
                "legs": len(record.legs),
            },
            symbol=record.instrument,
            correlation_id=key,
        )
        if self._on_completed is not None:
            try:
                self._on_completed(record)
            except Exception as exc:  # learning must never break recording, let alone trading
                self.stats.last_error = f"completion hook failed: {type(exc).__name__}: {exc}"
                self._emit(
                    AuditEventType.SINK_FAILED,
                    Severity.WARNING,
                    {"stage": "outcome_completion_hook", "trade_id": record.trade_id},
                    symbol=record.instrument,
                    correlation_id=key,
                )
        return record

    # ------------------------------------------------------ reconciliation / recovery

    def reconcile(
        self,
        positions: list[PositionInfo],
        *,
        tick_sizes: dict[str, float] | None = None,
        close_details: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Observe every live position, then finalize records whose position is gone.

        This is how a close that happened while the process was down still produces an outcome.
        ``close_details`` may carry broker closing-deal facts (real money, true exit price) for
        those tickets; without them the record says so instead of guessing.
        """
        live = {self.key_for(p) for p in positions}
        for position in positions:
            self.observe_position(position, tick_size=(tick_sizes or {}).get(position.symbol))
        finalized: list[str] = []
        for key, record in list(self._records.items()):
            if record.status is OutcomeStatus.CLOSED or key in live:
                continue
            tracker = self._trackers.get(key)
            details = (close_details or {}).get(key) or {}
            outcome = self.finalize(
                key,
                exit_price=(
                    details.get("exit_price")
                    if details.get("exit_price") is not None
                    else (tracker.last_price if tracker else None)
                ),
                cause=ExitCause.UNKNOWN,
                cause_source="recovery" if key in self._resumed else "external",
                realized_pnl=details.get("realized_pnl"),
                commission=details.get("commission"),
                swap=details.get("swap"),
                when=details.get("closed_at"),
                reason="position absent from broker truth",
            )
            if outcome is not None:
                self.stats.orphans_finalized += 1
                finalized.append(outcome.trade_id)
        return {
            "live_positions": len(live),
            "finalized": finalized,
            "open_outcomes": len(self.open_outcomes()),
        }

    # --------------------------------------------------------------------- storage

    async def flush(self) -> int:
        """Persist queued records. Failures stay queued for retry and never raise."""
        if self._pending and self.stats.persist_failures:
            self.stats.persist_retried += 1
        writer = self._storage()
        if writer is None:
            self.stats.pending_persist = len(self._pending)
            return 0
        persisted = 0
        while self._pending:
            key = self._pending[0]
            record = self._records.get(key)
            if record is None:
                self._pending.popleft()
                continue
            self.stats.persist_attempts += 1
            try:
                await writer(record)
            except Exception as exc:
                self.stats.persist_failures += 1
                self.stats.last_error = f"persist failed: {type(exc).__name__}: {exc}"
                self._emit(
                    AuditEventType.SINK_FAILED,
                    Severity.ERROR,
                    {"stage": "outcome_persist", "trade_id": record.trade_id, "error": str(exc)},
                    symbol=record.instrument,
                    correlation_id=key,
                )
                break  # keep the record queued: retry on the next flush
            self._pending.popleft()
            self.stats.persist_ok += 1
            self.stats.last_persist_at = self._clock().isoformat()
            persisted += 1
        self.stats.pending_persist = len(self._pending)
        return persisted

    async def resume(self) -> int:
        """Reload open records (so MAE/MFE survives restarts) and seed closed tickets."""
        getter = getattr(self.store, "get_open_outcomes", None)
        if not callable(getter):
            return 0
        try:
            open_rows = await getter()
        except Exception as exc:
            self.stats.last_error = f"resume failed: {type(exc).__name__}: {exc}"
            self._emit(
                AuditEventType.SINK_FAILED,
                Severity.ERROR,
                {"stage": "outcome_resume", "error": str(exc)},
            )
            return 0
        loaded = 0
        for record in open_rows:
            key = record.broker_ticket or record.trade_id
            self._records[key] = record
            tracker = ExcursionTracker(side=record.direction, entry=record.entry)
            tracker.restore(
                mae=record.mae,
                mfe=record.mfe,
                samples=record.excursion_samples,
                last_price=(record.evidence or {}).get("last_observed_price"),
            )
            self._trackers[key] = tracker
            tick_size = (record.evidence or {}).get("tick_size")
            if tick_size is not None:
                self._tick_sizes[key] = tick_size
            self._resumed.add(key)
            loaded += 1
        self.stats.recovered_open = loaded
        # Seed the closed tickets (bounded) so a finalized trade can never be re-opened.
        recent_getter = getattr(self.store, "get_outcomes", None)
        if callable(recent_getter):
            try:
                recent = await recent_getter(limit=500)
            except Exception:  # diagnostics only: never fatal
                recent = []
            for record in recent:
                if record.status is OutcomeStatus.CLOSED and record.broker_ticket:
                    self._closed.add(record.broker_ticket)
        return loaded

    # ----------------------------------------------------------------- introspection

    def get(self, key: str) -> HistoricalOutcome | None:
        return self._records.get(key)

    def open_outcomes(self) -> list[HistoricalOutcome]:
        return [r for r in self._records.values() if r.status is OutcomeStatus.OPEN]

    def completed_outcomes(self) -> list[HistoricalOutcome]:
        return [r for r in self._records.values() if r.status is OutcomeStatus.CLOSED]

    def all_outcomes(self) -> list[HistoricalOutcome]:
        return list(self._records.values())

    def summary(self) -> dict[str, Any]:
        """Dashboard view: counts by population, exit cause, and data availability."""
        completed = self.completed_outcomes()
        by_source: dict[str, int] = {}
        by_cause: dict[str, int] = {}
        for record in completed:
            by_source[record.source.value] = by_source.get(record.source.value, 0) + 1
            by_cause[record.exit_cause.value] = by_cause.get(record.exit_cause.value, 0) + 1
        last = max(completed, key=lambda r: r.exit_time or r.updated_at) if completed else None
        return {
            "open": len(self.open_outcomes()),
            "completed": len(completed),
            "autonomous": by_source.get(OutcomeSource.AUTONOMOUS.value, 0),
            "external": by_source.get(OutcomeSource.EXTERNAL.value, 0),
            "backtest": by_source.get(OutcomeSource.BACKTEST.value, 0),
            "by_exit_cause": dict(sorted(by_cause.items())),
            "mae_mfe_available": sum(1 for r in completed if r.excursion_samples > 0),
            "r_multiple_available": sum(1 for r in completed if r.r_multiple is not None),
            "realized_pnl_available": sum(
                1 for r in completed if r.evidence.get("realized_pnl_source") != "unavailable"
            ),
            "with_partial_exits": sum(1 for r in completed if len(r.legs) > 2),
            "last_completed": (
                {
                    "trade_id": last.trade_id,
                    "ticket": last.broker_ticket,
                    "symbol": last.instrument,
                    "strategy": last.strategy,
                    "source": last.source.value,
                    "exit_cause": last.exit_cause.value,
                    "exit_cause_source": last.exit_cause_source,
                    "realized_pnl": last.realized_pnl,
                    "r_multiple": last.r_multiple,
                    "exit_time": last.exit_time.isoformat() if last.exit_time else None,
                }
                if last is not None
                else None
            ),
            "recorder": self.stats.to_dict(),
        }
