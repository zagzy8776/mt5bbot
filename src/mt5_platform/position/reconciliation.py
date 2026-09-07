"""Position reconciliation with broker state.

The internal PositionState must match the broker's reported positions.
Any mismatch triggers reconciliation before further management actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import AuditEvent
from mt5_platform.position.models import POSITION_RECONCILIATION_MISMATCH, PositionState


@dataclass
class BrokerPosition:
    """Broker-reported position state."""

    ticket: str
    symbol: str
    side: OrderSide
    volume: float
    entry_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    unrealized_pnl: float
    opened_at: datetime


class ReconciliationResult:
    """Result of position reconciliation."""

    def __init__(
        self,
        *,
        matched: bool,
        internal: PositionState | None = None,
        broker: BrokerPosition | None = None,
        mismatches: list[str] | None = None,
    ) -> None:
        self.matched = matched
        self.internal = internal
        self.broker = broker
        self.mismatches = mismatches or []

    @property
    def has_mismatch(self) -> bool:
        return not self.matched


def reconcile_position(
    internal: PositionState,
    broker_positions: list[BrokerPosition],
    *,
    price_tolerance: float = 0.01,  # 1 pip tolerance for price
    volume_tolerance: float = 0.001,  # 0.001 lot tolerance
) -> ReconciliationResult:
    """Match internal position with broker position.

    Returns ReconciliationResult with match status and any mismatches.
    """
    # Find matching broker position by instrument and direction
    broker_match: BrokerPosition | None = None
    for bp in broker_positions:
        if bp.symbol == internal.instrument and bp.side == internal.direction:
            broker_match = bp
            break

    if broker_match is None:
        return ReconciliationResult(
            matched=False,
            internal=internal,
            broker=None,
            mismatches=["no matching broker position found"],
        )

    mismatches: list[str] = []

    # Compare volumes
    if abs(internal.volume - broker_match.volume) > volume_tolerance:
        mismatches.append(
            f"volume mismatch: internal={internal.volume}, broker={broker_match.volume}"
        )

    # Compare entry price
    if abs(internal.entry_price - broker_match.entry_price) > price_tolerance:
        mismatches.append(
            f"entry price mismatch: internal={internal.entry_price}, "
            f"broker={broker_match.entry_price}"
        )

    # Compare stop loss
    if internal.stop_loss is not None and broker_match.stop_loss is not None:
        if abs(internal.stop_loss - broker_match.stop_loss) > price_tolerance:
            mismatches.append(
                f"stop loss mismatch: internal={internal.stop_loss}, "
                f"broker={broker_match.stop_loss}"
            )
    elif internal.stop_loss != broker_match.stop_loss:
        mismatches.append(
            f"stop loss presence mismatch: internal={internal.stop_loss}, "
            f"broker={broker_match.stop_loss}"
        )

    # Compare take profit
    if internal.take_profit is not None and broker_match.take_profit is not None:
        if abs(internal.take_profit - broker_match.take_profit) > price_tolerance:
            mismatches.append(
                f"take profit mismatch: internal={internal.take_profit}, "
                f"broker={broker_match.take_profit}"
            )
    elif internal.take_profit != broker_match.take_profit:
        mismatches.append(
            f"take profit presence mismatch: internal={internal.take_profit}, "
            f"broker={broker_match.take_profit}"
        )

    matched = len(mismatches) == 0
    return ReconciliationResult(
        matched=matched,
        internal=internal,
        broker=broker_match,
        mismatches=mismatches,
    )


def emit_reconciliation_mismatch(result: ReconciliationResult, correlation_id: str) -> None:
    """Emit audit event for reconciliation mismatch."""
    if result.has_mismatch:
        audit_log.emit(
            AuditEvent(
                component="reconciliation",
                event_type=POSITION_RECONCILIATION_MISMATCH,
                severity="warning",
                symbol=result.internal.instrument
                if result.internal
                else result.broker.symbol
                if result.broker
                else "unknown",
                correlation_id=correlation_id,
                payload={
                    "internal_position_id": result.internal.position_id
                    if result.internal
                    else None,
                    "broker_ticket": result.broker.ticket if result.broker else None,
                    "mismatches": result.mismatches,
                },
            )
        )


__all__ = [
    "BrokerPosition",
    "ReconciliationResult",
    "reconcile_position",
    "emit_reconciliation_mismatch",
]
