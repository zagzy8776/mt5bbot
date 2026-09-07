"""Confidence calibration tracker (Phase E).

Tracks whether agent confidence actually corresponds to observed reliability.
If an agent repeatedly says confidence=0.90 but is only correct 55% of
the time, this module records that gap.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mt5_platform.learning.models import AgentPerformanceRecord


class ConfidenceCalibrationTracker:
    """Tracks confidence calibration per agent."""

    def __init__(self) -> None:
        self._records: dict[str, AgentPerformanceRecord] = {}

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
        if agent_name not in self._records:
            self._records[agent_name] = AgentPerformanceRecord(agent_name=agent_name)
        rec = self._records[agent_name]
        rec.total_opinions += 1
        rec.confidence_sum += confidence
        if correct:
            rec.correct_directional += 1
            rec.confidence_correct_sum += confidence
        else:
            rec.incorrect_directional += 1
        rec.last_updated = datetime.now(tz=__import__("datetime").timezone.utc)

        if regime:
            rec.by_regime.setdefault(regime, {"correct": 0, "total": 0})
            rec.by_regime[regime]["total"] += 1
            if correct:
                rec.by_regime[regime]["correct"] += 1
        if instrument:
            rec.by_instrument.setdefault(instrument, {"correct": 0, "total": 0})
            rec.by_instrument[instrument]["total"] += 1
            if correct:
                rec.by_instrument[instrument]["correct"] += 1
        if timeframe:
            rec.by_timeframe.setdefault(timeframe, {"correct": 0, "total": 0})
            rec.by_timeframe[timeframe]["total"] += 1
            if correct:
                rec.by_timeframe[timeframe]["correct"] += 1

    def get_calibration(self, agent_name: str) -> dict[str, Any] | None:
        rec = self._records.get(agent_name)
        if rec is None or rec.total_opinions == 0:
            return None
        accuracy = rec.correct_directional / rec.total_opinions
        avg_confidence = rec.confidence_sum / rec.total_opinions
        calibration_gap = abs(avg_confidence - accuracy)
        return {
            "agent_name": agent_name,
            "total_opinions": rec.total_opinions,
            "accuracy": accuracy,
            "avg_confidence": avg_confidence,
            "calibration_gap": calibration_gap,
            "well_calibrated": calibration_gap < 0.15,
            "by_regime": rec.by_regime,
            "by_instrument": rec.by_instrument,
            "by_timeframe": rec.by_timeframe,
        }

    def record_caution(self, agent_name: str) -> None:
        if agent_name not in self._records:
            self._records[agent_name] = AgentPerformanceRecord(agent_name=agent_name)
        self._records[agent_name].caution_calls += 1
        self._records[agent_name].last_updated = datetime.now(
            tz=__import__("datetime").timezone.utc
        )

    def record_no_trade(self, agent_name: str) -> None:
        if agent_name not in self._records:
            self._records[agent_name] = AgentPerformanceRecord(agent_name=agent_name)
        self._records[agent_name].no_trade_calls += 1
        self._records[agent_name].last_updated = datetime.now(
            tz=__import__("datetime").timezone.utc
        )

    def get_record(self, agent_name: str) -> AgentPerformanceRecord | None:
        return self._records.get(agent_name)

    def all_records(self) -> list[AgentPerformanceRecord]:
        return list(self._records.values())


__all__ = ["ConfidenceCalibrationTracker"]
