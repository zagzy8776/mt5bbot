"""Agent performance tracker (Phase E).

Tracks per-agent performance across regimes, instruments, and timeframes.
Does NOT automatically adjust weights — that requires explicit validation.
"""

from __future__ import annotations

from typing import Any

from mt5_platform.learning.calibration import ConfidenceCalibrationTracker
from mt5_platform.learning.models import AgentPerformanceRecord


class AgentPerformanceTracker:
    """Aggregates agent performance for review, not automatic mutation."""

    def __init__(self) -> None:
        self._calibration = ConfidenceCalibrationTracker()

    def record_opinion(
        self,
        agent_name: str,
        confidence: float,
        correct: bool,
        *,
        regime: str | None = None,
        instrument: str | None = None,
        timeframe: str | None = None,
    ) -> None:
        self._calibration.record_opinion(
            agent_name,
            confidence,
            correct,
            regime=regime,
            instrument=instrument,
            timeframe=timeframe,
        )

    def record_caution(self, agent_name: str) -> None:
        self._calibration.record_caution(agent_name)

    def record_no_trade(self, agent_name: str) -> None:
        self._calibration.record_no_trade(agent_name)

    def get_performance(self, agent_name: str) -> dict[str, Any] | None:
        return self._calibration.get_calibration(agent_name)

    def all_performance(self) -> dict[str, dict[str, Any]]:
        result = {}
        for rec in self._calibration.all_records():
            cal = self._calibration.get_calibration(rec.agent_name)
            if cal:
                result[rec.agent_name] = cal
        return result

    def get_record(self, agent_name: str) -> AgentPerformanceRecord | None:
        return self._calibration.get_record(agent_name)


__all__ = ["AgentPerformanceTracker"]
