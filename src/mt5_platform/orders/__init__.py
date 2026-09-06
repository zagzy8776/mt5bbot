"""Order manager and state machine. Does not decide trade quality — risk does.

Phase 6: lifecycle orchestration with audit trail, store persistence, adapter
submission and broker reconciliation. Local state is suspicion; broker state
is truth.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Any

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, OrderStatus, Severity
from mt5_platform.common.events import (
    AuditEvent,
    ExecutionRecord,
    OrderRequest,
    RiskDecision,
    StrategySignal,
    utc_now,
)
from mt5_platform.config import Settings
from mt5_platform.risk import RiskContext, RiskEngine
from mt5_platform.storage.base import MarketDataStore

if TYPE_CHECKING:
    from mt5_platform.execution import ExecutionAdapter

ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.RISK_CHECK, OrderStatus.CANCELLED, OrderStatus.FAILED},
    OrderStatus.RISK_CHECK: {
        OrderStatus.APPROVED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
    OrderStatus.APPROVED: {OrderStatus.SUBMITTED, OrderStatus.CANCELLED, OrderStatus.FAILED},
    OrderStatus.SUBMITTED: {
        OrderStatus.ACCEPTED,
        OrderStatus.BROKER_REJECTED,
        OrderStatus.FAILED,
    },
    OrderStatus.ACCEPTED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
    OrderStatus.FILLED: {OrderStatus.CLOSED},
    OrderStatus.REJECTED: set(),
    OrderStatus.BROKER_REJECTED: set(),
    OrderStatus.CLOSED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.FAILED: set(),
}


class InvalidTransitionError(ValueError):
    pass


class OrderSubmissionBlocked(RuntimeError):
    """Emergency kill switch engaged — new order submission is forbidden."""


# Orders in these states still need reconciliation against broker truth.
ACTIVE_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.CREATED,
        OrderStatus.RISK_CHECK,
        OrderStatus.APPROVED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
    }
)

_TRANSITION_EVENTS: dict[OrderStatus, tuple[AuditEventType, Severity]] = {
    OrderStatus.SUBMITTED: (AuditEventType.ORDER_SUBMITTED, Severity.INFO),
    OrderStatus.REJECTED: (AuditEventType.ORDER_REJECTED, Severity.WARNING),
    OrderStatus.BROKER_REJECTED: (AuditEventType.ORDER_REJECTED, Severity.WARNING),
    OrderStatus.FILLED: (AuditEventType.ORDER_FILLED, Severity.INFO),
    OrderStatus.PARTIALLY_FILLED: (AuditEventType.ORDER_FILLED, Severity.INFO),
    OrderStatus.CANCELLED: (AuditEventType.ORDER_CANCELLED, Severity.INFO),
    OrderStatus.FAILED: (AuditEventType.EXECUTION_ERROR, Severity.ERROR),
}


@dataclass
class OrderManagerStats:
    created: int = 0
    submitted: int = 0
    filled: int = 0
    partially_filled: int = 0
    broker_rejected: int = 0
    risk_rejected: int = 0
    cancelled: int = 0
    failed: int = 0
    invalid_transitions: int = 0
    reconciliation_mismatches: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(sorted(vars(self).items()))


@dataclass
class OrderManager:
    settings: Settings | None = None
    store: MarketDataStore | None = None
    risk_engine: RiskEngine | None = None
    _orders: dict[str, OrderRequest] = field(default_factory=dict)
    _history: dict[str, deque[dict[str, Any]]] = field(default_factory=dict)
    _stats: OrderManagerStats = field(default_factory=OrderManagerStats)
    _lock: RLock = field(default_factory=RLock)

    def create_from_signal(self, signal: StrategySignal, volume: float) -> OrderRequest:
        if volume <= 0:
            raise ValueError("volume must be positive")
        order = OrderRequest(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.direction,
            volume=volume,
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            status=OrderStatus.CREATED,
            correlation_id=signal.correlation_id,
        )
        with self._lock:
            self._orders[order.order_id] = order
            self._stats.created += 1
        self._audit(
            AuditEventType.ORDER_CREATED,
            Severity.INFO,
            order,
            payload={
                "volume": volume,
                "signal_id": signal.signal_id,
                "side": signal.direction.value,
            },
        )
        return order

    def transition(self, order_id: str, new_status: OrderStatus) -> OrderRequest:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"unknown order: {order_id}")
            allowed = ALLOWED_TRANSITIONS.get(order.status, set())
            if new_status not in allowed:
                self._stats.invalid_transitions += 1
                self._audit(
                    AuditEventType.ORDER_TRANSITION_REJECTED,
                    Severity.WARNING,
                    order,
                    payload={"from": order.status.value, "to": new_status.value},
                )
                raise InvalidTransitionError(
                    f"Cannot transition {order.status.value} -> {new_status.value}"
                )
            return self._set_status_locked(order, new_status, reason=None)

    def force_transition(
        self, order_id: str, new_status: OrderStatus, *, reason: str
    ) -> OrderRequest:
        """Bypass the state machine — reconciliation only. Broker state is truth."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"unknown order: {order_id}")
            return self._set_status_locked(order, new_status, reason=reason)

    def _set_status_locked(
        self, order: OrderRequest, new_status: OrderStatus, *, reason: str | None
    ) -> OrderRequest:
        previous = order.status
        order.status = new_status
        order.updated_at = utc_now()
        history = self._history.setdefault(order.order_id, deque(maxlen=100))
        history.append(
            {
                "at": order.updated_at.isoformat(),
                "from": previous.value,
                "to": new_status.value,
                "forced": reason is not None,
                "reason": reason,
            }
        )
        self._count_status(new_status)
        self._audit_status_change(order, previous, new_status, reason)
        return order

    def _count_status(self, new_status: OrderStatus) -> None:
        stats = self._stats
        if new_status is OrderStatus.CREATED:
            stats.created += 1
        elif new_status is OrderStatus.SUBMITTED:
            stats.submitted += 1
        elif new_status is OrderStatus.FILLED:
            stats.filled += 1
        elif new_status is OrderStatus.PARTIALLY_FILLED:
            stats.partially_filled += 1
        elif new_status is OrderStatus.BROKER_REJECTED:
            stats.broker_rejected += 1
        elif new_status is OrderStatus.REJECTED:
            stats.risk_rejected += 1
        elif new_status is OrderStatus.CANCELLED:
            stats.cancelled += 1
        elif new_status is OrderStatus.FAILED:
            stats.failed += 1

    def _audit_status_change(
        self,
        order: OrderRequest,
        previous: OrderStatus,
        new_status: OrderStatus,
        reason: str | None,
    ) -> None:
        mapped = _TRANSITION_EVENTS.get(new_status)
        if mapped is None:
            return
        event_type, severity = mapped
        payload: dict[str, Any] = {"from": previous.value, "to": new_status.value}
        if reason:
            payload["reason"] = reason
        self._audit(event_type, severity, order, payload=payload)

    def apply_risk_decision(self, order_id: str, decision: RiskDecision) -> OrderRequest:
        self.transition(order_id, OrderStatus.RISK_CHECK)
        if decision.approved:
            return self.transition(order_id, OrderStatus.APPROVED)
        order = self.transition(order_id, OrderStatus.REJECTED)
        self._audit(
            AuditEventType.ORDER_REJECTED,
            Severity.WARNING,
            order,
            payload={"reasons": decision.reasons},
        )
        return order

    def get(self, order_id: str) -> OrderRequest | None:
        with self._lock:
            return self._orders.get(order_id)

    def all_orders(self) -> list[OrderRequest]:
        with self._lock:
            return list(self._orders.values())

    def recent_orders(self, limit: int = 50) -> list[OrderRequest]:
        orders = sorted(self.all_orders(), key=lambda o: o.created_at, reverse=True)
        return orders[: max(limit, 0)]

    def orders_by_status(self, status: OrderStatus) -> list[OrderRequest]:
        return [o for o in self.all_orders() if o.status is status]

    def history(self, order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._history.get(order_id, ())]

    def stats_snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = Counter(o.status.value for o in self._orders.values())
            return {
                "total": len(self._orders),
                "orders_by_status": dict(sorted(counts.items())),
                "stats": self._stats.to_dict(),
            }

    def _kill_switch_engaged(self) -> bool:
        engaged = bool(self.settings.emergency_kill_switch) if self.settings else False
        if self.risk_engine is not None:
            engaged = engaged or self.risk_engine.kill_switch
        return engaged

    async def _persist_order(self, order_id: str) -> None:
        if self.store is None:
            return
        if self.settings is not None and not self.settings.order_store_sink_enabled:
            return
        order = self.get(order_id)
        if order is not None:
            await self.store.write_order(order)

    async def _persist_execution(self, record: ExecutionRecord) -> None:
        if self.store is None:
            return
        if self.settings is not None and not self.settings.order_store_sink_enabled:
            return
        await self.store.write_execution(record)

    async def submit_order(
        self, order_id: str, adapter: ExecutionAdapter
    ) -> ExecutionRecord:
        """Submit an already risk-APPROVED order. Refuses under kill switch."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"unknown order: {order_id}")
            if order.status is not OrderStatus.APPROVED:
                raise ValueError(
                    f"only APPROVED orders may be submitted (status={order.status.value})"
                )
            if self._kill_switch_engaged():
                raise OrderSubmissionBlocked(
                    "emergency kill switch engaged — order submission forbidden"
                )
        self.transition(order_id, OrderStatus.SUBMITTED)
        current = self.get(order_id)
        if current is None:  # pragma: no cover - defensive
            raise KeyError(f"unknown order: {order_id}")
        try:
            record = await adapter.submit_order(current)
        except Exception as exc:
            self.transition(order_id, OrderStatus.FAILED)
            failed = self.get(order_id)
            if failed is not None:
                self._audit(
                    AuditEventType.EXECUTION_ERROR,
                    Severity.ERROR,
                    failed,
                    payload={"stage": "adapter_submit"},
                    error=str(exc),
                )
            await self._persist_order(order_id)
            raise
        await self.apply_execution(order_id, record)
        await self._persist_order(order_id)
        await self._persist_execution(record)
        return record

    async def process_signal(
        self,
        signal: StrategySignal,
        ctx: RiskContext,
        adapter: ExecutionAdapter,
    ) -> tuple[OrderRequest, RiskDecision, ExecutionRecord | None]:
        """Full pipeline: create order → risk gate → submit if approved."""
        volume = ctx.proposed_volume
        if volume is None or volume <= 0:
            raise ValueError(
                "RiskContext.proposed_volume must be set (>0) to create an order"
            )
        order = self.create_from_signal(signal, volume=volume)
        if self.risk_engine is not None:
            decision = self.risk_engine.evaluate(signal, ctx)
        else:
            # Fail closed: no order proceeds without a risk decision.
            decision = RiskDecision(
                approved=False,
                reasons=["no_risk_engine_configured"],
                correlation_id=signal.correlation_id,
            )
        self.apply_risk_decision(order.order_id, decision)
        await self._persist_order(order.order_id)
        if not decision.approved:
            final = self.get(order.order_id)
            if final is None:  # pragma: no cover - defensive
                raise KeyError(f"unknown order: {order.order_id}")
            return final, decision, None
        record = await self.submit_order(order.order_id, adapter)
        final = self.get(order.order_id)
        if final is None:  # pragma: no cover - defensive
            raise KeyError(f"unknown order: {order.order_id}")
        return final, decision, record

    async def apply_execution(
        self, order_id: str, record: ExecutionRecord
    ) -> OrderRequest:
        """Fold a broker execution record into local order state."""
        final = record.final_status
        if final in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
            with self._lock:
                order = self._orders.get(order_id)
                if order is not None and order.status is OrderStatus.SUBMITTED:
                    self.transition(order_id, OrderStatus.ACCEPTED)
            order = self.transition(order_id, final)
        elif final is OrderStatus.BROKER_REJECTED:
            order = self.transition(order_id, OrderStatus.BROKER_REJECTED)
        else:
            with self._lock:
                order = self._orders[order_id]
        with self._lock:
            if final is OrderStatus.FILLED:
                order.filled_volume = record.filled_volume or record.requested_volume
            elif final is OrderStatus.PARTIALLY_FILLED:
                order.filled_volume = record.filled_volume
            if record.rejection_reason:
                order.rejection_reason = record.rejection_reason
        await self._persist_order(order_id)
        updated = self.get(order_id)
        if updated is None:  # pragma: no cover - defensive
            raise KeyError(f"unknown order: {order_id}")
        return updated

    async def reconcile(self, adapter: ExecutionAdapter) -> dict[str, Any]:
        """Reconcile active local orders against broker truth.

        NEVER trust local state after a crash: any divergence is corrected to
        the broker's answer and audited.
        """
        with self._lock:
            active = [o for o in self._orders.values() if o.status in ACTIVE_STATUSES]
        broker_states = await adapter.broker_order_states([o.order_id for o in active])
        mismatches: list[dict[str, str]] = []
        for order in active:
            raw = broker_states.get(order.order_id)
            if raw is None:
                continue
            broker_status = OrderStatus(raw)
            if broker_status is not order.status:
                mismatches.append(
                    {
                        "order_id": order.order_id,
                        "local": order.status.value,
                        "broker": broker_status.value,
                    }
                )
                self.force_transition(
                    order.order_id, broker_status, reason="broker_reconciliation"
                )
        if mismatches:
            self._stats.reconciliation_mismatches += len(mismatches)
        report: dict[str, Any] = {
            "checked": len(active),
            "mismatches": mismatches,
            "reconciled_at": utc_now().isoformat(),
        }
        audit_log.emit(
            AuditEvent(
                component="orders",
                event_type=(
                    AuditEventType.RECONCILIATION_MISMATCH
                    if mismatches
                    else AuditEventType.RECONCILIATION_COMPLETED
                ).value,
                severity=Severity.WARNING if mismatches else Severity.INFO,
                payload=report,
            )
        )
        return report

    @staticmethod
    def _audit(
        event_type: AuditEventType,
        severity: Severity,
        order: OrderRequest,
        *,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        audit_log.emit(
            AuditEvent(
                component="orders",
                event_type=event_type.value,
                severity=severity,
                symbol=order.symbol,
                correlation_id=order.correlation_id,
                payload={"order_id": order.order_id, **(payload or {})},
                error=error,
            )
        )


__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "InvalidTransitionError",
    "OrderManager",
    "OrderManagerStats",
    "OrderSubmissionBlocked",
]
