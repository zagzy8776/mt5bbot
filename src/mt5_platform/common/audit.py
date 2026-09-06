"""In-memory audit sink for Phase 1. Persistence arrives in Phase 3/14."""

from __future__ import annotations

from collections import deque
from threading import Lock

from mt5_platform.common.events import AuditEvent


class AuditLog:
    """Thread-safe ring buffer of audit events."""

    def __init__(self, maxlen: int = 10_000) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=maxlen)
        self._lock = Lock()

    def emit(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def recent(self, limit: int = 100) -> list[AuditEvent]:
        with self._lock:
            items = list(self._events)
        if limit <= 0:
            return []
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


# Process-wide default sink (swap for DB-backed sink later)
audit_log = AuditLog()
