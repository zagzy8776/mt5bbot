"""Position Intelligence package (Phase D).

Position Manager evaluates open positions against current market evidence
and the original trade thesis. It produces explicit, traceable decisions
that flow through RiskEngine -> OrderManager -> Execution.
"""

from mt5_platform.position.manager import PositionManager, PositionManagerConfig
from mt5_platform.position.models import (
    POSITION_DECISION_EMITTED,
    POSITION_EMERGENCY_TRIGGERED,
    POSITION_RECONCILIATION_MISMATCH,
    POSITION_THESIS_INVALIDATED,
    InvalidationReason,
    PositionDecisionModel,
    PositionDecisionRequest,
    PositionManagerAudit,
    PositionState,
    ThesisSnapshot,
)
from mt5_platform.position.reconciliation import (
    BrokerPosition,
    ReconciliationResult,
    emit_reconciliation_mismatch,
    reconcile_position,
)

__all__ = [
    "BrokerPosition",
    "InvalidationReason",
    "PositionDecisionModel",
    "PositionDecisionRequest",
    "PositionManager",
    "PositionManagerAudit",
    "PositionManagerConfig",
    "PositionState",
    "ReconciliationResult",
    "ThesisSnapshot",
    "ThesisStatus",
    "emit_reconciliation_mismatch",
    "reconcile_position",
    "POSITION_DECISION_EMITTED",
    "POSITION_THESIS_INVALIDATED",
    "POSITION_RECONCILIATION_MISMATCH",
    "POSITION_EMERGENCY_TRIGGERED",
]
