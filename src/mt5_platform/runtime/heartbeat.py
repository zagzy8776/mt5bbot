"""Read-only runtime heartbeat: is the loop turning, and is the data fresh?

This module exists to answer one question with evidence instead of inference:

    is the bot evaluating closed candles, stale, blocked, or not running at all?

It is observational by construction. It reads the runtime's in-memory snapshot and computes
freshness from the clock; it cannot start, stop, pause, size or submit anything, and it never writes
state. Missing counters are reported as ``None`` ("unknown") rather than guessed, because a health
report that invents values is worse than no report.

Bar convention (the part that must not be ambiguous)
----------------------------------------------------
MT5 exposes bar index 0 as the *forming* bar and index 1 as the most recently completed bar, and a
bar's timestamp is its **open** time. So at 19:07 UTC on M15:

    latest closed bar   opened 18:45, closed 19:00
    forming bar         opened 19:00 (index 0, not yet closed)

A correct ``last_processed_bar`` therefore reads ``18:45``, and freshness is judged against *that*
bar — never against "is the timestamp recent". The payload reports both the open and the close of
every bar it mentions, plus ``bar_age_seconds`` measured **from the close** of the processed bar, so
a 19:07 reading with a processed bar of 18:45 shows an age of 7 minutes.

A short grace window covers the moment just after a boundary: the loop has not had its next
poll yet, so the previous bar is still acceptable until ``grace_seconds`` have passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_GRACE_SECONDS = 60.0

_TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}
_UNIT_SECONDS = {"M": 60, "H": 3600, "D": 86400, "W": 604800}
_TIMEFRAME_PATTERN = re.compile(r"^(\d+)\s*([MHDW])$")


def timeframe_seconds(timeframe: str) -> int | None:
    """Seconds per bar for a timeframe label, or None when the label is not understood."""
    label = (timeframe or "").strip().upper()
    if label in _TIMEFRAME_SECONDS:
        return _TIMEFRAME_SECONDS[label]
    match = _TIMEFRAME_PATTERN.match(label)
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2)
    if count <= 0:
        return None
    return count * _UNIT_SECONDS[unit]


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp (or datetime) into an aware UTC datetime; None when unusable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class BarClock:
    """Which bar should have closed, and which bar must have been processed to count as fresh."""

    timeframe_seconds: int
    latest_closed_bar_open: datetime  # label of the most recent completed bar (18:45 at 19:07)
    latest_closed_bar_close: datetime  # 19:00
    freshness_floor: datetime  # latest_closed_bar_open, relaxed by one bar inside the grace window
    seconds_since_close: float
    within_grace: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe_seconds": self.timeframe_seconds,
            "latest_closed_bar_open": self.latest_closed_bar_open.isoformat(),
            "latest_closed_bar_close": self.latest_closed_bar_close.isoformat(),
            "freshness_floor": self.freshness_floor.isoformat(),
            "seconds_since_close": round(self.seconds_since_close, 3),
            "within_grace": self.within_grace,
        }


def bar_clock(
    now: datetime, timeframe: str, *, grace_seconds: float = DEFAULT_GRACE_SECONDS
) -> BarClock | None:
    """The bar clock for ``timeframe`` at ``now`` (None when the timeframe is unknown).

    ``now`` is floored to the boundary we are inside; that boundary is the close of the most recent
    completed bar, whose label (open time) is one timeframe earlier.
    """
    seconds = timeframe_seconds(timeframe)
    if seconds is None:
        return None
    stamp = now if now.tzinfo else now.replace(tzinfo=UTC)
    epoch = int(stamp.timestamp())
    boundary = epoch - (epoch % seconds)  # close time of the latest completed bar
    close_at = datetime.fromtimestamp(boundary, tz=UTC)
    latest_open = close_at - timedelta(seconds=seconds)
    seconds_since_close = (stamp - close_at).total_seconds()
    within_grace = seconds_since_close < grace_seconds
    floor = latest_open - timedelta(seconds=seconds) if within_grace else latest_open
    return BarClock(
        timeframe_seconds=seconds,
        latest_closed_bar_open=latest_open,
        latest_closed_bar_close=close_at,
        freshness_floor=floor,
        seconds_since_close=seconds_since_close,
        within_grace=within_grace,
    )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_runtime_heartbeat(
    snapshot: dict[str, Any],
    *,
    risk_snapshot: dict[str, Any] | None = None,
    now: datetime | None = None,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> dict[str, Any]:
    """Assemble the read-only heartbeat payload from the runtime's in-memory snapshot.

    ``assessment`` is the point of the endpoint — one of ``not_running``, ``stale_data``,
    ``execution_blocked``, ``setups_rejected_by_risk``, ``healthy_waiting_for_setup``,
    ``healthy`` or ``unknown``. A healthy bot with no setup is expected behaviour, not a fault, so
    the payload separates "waiting" from "stale" instead of collapsing both into "no trades".
    """
    stamp = now or datetime.now(UTC)
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    state = str(snapshot.get("state", "") or "")
    symbol = str(snapshot.get("symbol", "") or "")
    timeframe = str(snapshot.get("timeframe", "") or "")
    stats = dict(snapshot.get("stats") or {})
    pipeline = dict(stats.get("pipeline") or {})
    engine = dict(stats.get("signal_engine") or {})
    execution = dict(stats.get("execution") or {})
    terminal = dict(execution.get("terminal") or {})
    risk_stats = dict((risk_snapshot or {}).get("stats") or {})

    notes: list[str] = []
    clock = bar_clock(stamp, timeframe, grace_seconds=grace_seconds)
    if clock is None:
        notes.append(f"freshness not computed: timeframe {timeframe!r} is missing or unknown")

    last_processed = parse_timestamp(pipeline.get("last_processed_bar"))
    bar_close_at = (
        last_processed + timedelta(seconds=clock.timeframe_seconds)
        if last_processed is not None and clock is not None
        else None
    )
    # Age is measured from the processed bar's CLOSE (19:07 vs a 18:45-labelled bar = 7 minutes).
    bar_age_seconds = (stamp - bar_close_at).total_seconds() if bar_close_at is not None else None
    bar_open_age_seconds = (
        (stamp - last_processed).total_seconds() if last_processed is not None else None
    )
    lag_bars = (
        (clock.latest_closed_bar_open - last_processed).total_seconds() / clock.timeframe_seconds
        if clock is not None and last_processed is not None
        else None
    )

    data_fresh: bool | None
    if state != "running":
        data_fresh = None
        notes.append("runtime is not running: freshness is not judged")
    elif clock is None or last_processed is None:
        data_fresh = None
        notes.append("no processed bar yet: freshness unknown")
    else:
        data_fresh = last_processed >= clock.freshness_floor
        if clock.within_grace:
            notes.append("within the post-close grace window: the previous bar is acceptable")

    generated = _int(engine.get("signals_generated"))
    rejected = _int(engine.get("signals_rejected"))
    orders_sent = _int(stats.get("orders_sent"))
    risk_rejected = _int(risk_stats.get("rejected"))
    blocked = bool(execution.get("blocked")) if "blocked" in execution else None

    if state != "running":
        assessment = "not_running"
    elif data_fresh is False:
        assessment = "stale_data"
    elif blocked:
        assessment = "execution_blocked"
    elif (risk_rejected or 0) > 0 and (orders_sent or 0) == 0:
        assessment = "setups_rejected_by_risk"
    elif data_fresh and (generated or 0) == 0 and (orders_sent or 0) == 0:
        assessment = "healthy_waiting_for_setup"
    elif data_fresh:
        assessment = "healthy"
    else:
        assessment = "unknown"

    return {
        "observed_at": stamp.isoformat(),
        "read_only": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "state": state,
        "connected": bool(snapshot.get("connected")) if "connected" in snapshot else None,
        "started_at": snapshot.get("started_at"),
        "cycle": _int(pipeline.get("cycle")),
        "cycles": _int(stats.get("cycles")),
        "bars_processed": _int(stats.get("bars_processed")),
        "signals": _int(stats.get("signals")),
        "orders_sent": orders_sent,
        "errors": _int(stats.get("errors")),
        "consecutive_errors": _int(stats.get("consecutive_errors")),
        "pipeline_stage": str(stats.get("pipeline_stage", "") or ""),
        "last_processed_bar": last_processed.isoformat() if last_processed else None,
        "last_processed_bar_close": bar_close_at.isoformat() if bar_close_at else None,
        "latest_closed_bar_open": (
            clock.latest_closed_bar_open.isoformat() if clock is not None else None
        ),
        "latest_closed_bar_close": (
            clock.latest_closed_bar_close.isoformat() if clock is not None else None
        ),
        "bar_convention": (
            "bar timestamps are the bar's OPEN time (MT5 index 0 is the forming bar); a fresh loop "
            "has last_processed_bar == latest_closed_bar_open"
        ),
        "freshness_floor": clock.freshness_floor.isoformat() if clock is not None else None,
        "bar_age_seconds": round(bar_age_seconds, 3) if bar_age_seconds is not None else None,
        "bar_open_age_seconds": (
            round(bar_open_age_seconds, 3) if bar_open_age_seconds is not None else None
        ),
        "lag_bars": round(lag_bars, 3) if lag_bars is not None else None,
        "data_fresh": data_fresh,
        "waiting": bool(pipeline.get("waiting")) if "waiting" in pipeline else None,
        "poll_s": _float(pipeline.get("poll_s")),
        "signal_engine": {
            "evaluations": _int(engine.get("evaluations")),
            "generated": generated,
            "rejected": rejected,
            "events_processed": _int(engine.get("events_processed")),
            "strategy_errors": _int(engine.get("strategy_errors")),
            "last_evaluation_time": engine.get("last_evaluation_time"),
            "last_rejection": engine.get("last_rejection"),
        },
        "risk": {
            "checks": _int(risk_stats.get("checks")),
            "approved": _int(risk_stats.get("approved")),
            "rejected": risk_rejected,
            "reject_reasons": dict(risk_stats.get("reject_reasons") or {}),
        },
        "execution": {
            "blocked": blocked,
            "trade_allowed": terminal.get("trade_allowed"),
            "reason": str(terminal.get("reason", "") or ""),
        },
        "assessment": assessment,
        "notes": notes,
    }


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "BarClock",
    "bar_clock",
    "build_runtime_heartbeat",
    "parse_timestamp",
    "timeframe_seconds",
]
