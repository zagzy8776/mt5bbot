"""Exit-cause resolution.

The component that performs an exit states the cause. When the runtime did not perform the exit
(an external/manual close, a broker stop, a recovery after downtime), the exit price is matched
against the recorded levels — never against profit/loss. An unknowable close stays UNKNOWN.
"""

from __future__ import annotations

from typing import Any

from mt5_platform.common.enums import ExitCause, PositionDecision

_REASON_RULES: tuple[tuple[tuple[str, ...], ExitCause], ...] = (
    (("thesis", "invalidat"), ExitCause.THESIS_INVALIDATION),
    (("opposite",), ExitCause.OPPOSITE_SIGNAL),
    (("volatil", "spike"), ExitCause.VOLATILITY_EXIT),
    (("time", "expire", "stale"), ExitCause.TIME_STOP),
    (("trail",), ExitCause.TRAILING_STOP),
    (("break", "even"), ExitCause.BREAK_EVEN),
    (("target", "take", "profit"), ExitCause.TAKE_PROFIT),
    (("stop",), ExitCause.STOP_LOSS),
)


def cause_from_position_decision(decision: Any) -> tuple[ExitCause, str]:
    """Map a PositionManager decision onto the taxonomy using its explicit reason text."""
    action = PositionDecision(decision.decision)
    reason = str(getattr(decision, "reason", "") or "").lower()
    if action is PositionDecision.EMERGENCY_EXIT:
        return ExitCause.EMERGENCY_EXIT, "runtime"
    if action is PositionDecision.REDUCE:
        return ExitCause.PARTIAL_EXIT, "runtime"
    if action is not PositionDecision.EXIT:
        return ExitCause.UNKNOWN, "not_an_exit_decision"
    for keywords, cause in _REASON_RULES:
        if all(keyword in reason for keyword in keywords):
            return cause, "decision_reason"
    return ExitCause.UNKNOWN, "decision_reason_unmatched"


def cause_from_levels(
    *,
    exit_price: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    tolerance: float | None,
) -> tuple[ExitCause, str]:
    """Match the exit price against the recorded protection levels (never against P/L)."""
    if exit_price is None:
        return ExitCause.UNKNOWN, "external_unknown"
    if tolerance is None:
        return ExitCause.UNKNOWN, "external_unknown"
    tolerance = abs(float(tolerance))
    if stop_loss and abs(float(exit_price) - float(stop_loss)) <= tolerance:
        return ExitCause.STOP_LOSS, "level_match"
    if take_profit and abs(float(exit_price) - float(take_profit)) <= tolerance:
        return ExitCause.TAKE_PROFIT, "level_match"
    return ExitCause.UNKNOWN, "external_unknown"


def level_tolerance(tick_size: float | None, *, ticks: float = 2.0) -> float | None:
    """Tolerance used for level matching: a few broker ticks, or None when unknown."""
    if tick_size is None:
        return None
    try:
        size = float(tick_size)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    return size * ticks
