"""Stateful proxy manager with circuit-breaker recovery (M2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock

from mt5_platform.common.enums import ProxyErrorType, ProxyState


@dataclass
class ProxyRecord:
    proxy_id: str
    url: str
    state: ProxyState = ProxyState.HEALTHY
    latency_ms: float | None = None
    successful_requests: int = 0
    failed_requests: int = 0
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    cooldown_until: datetime | None = None
    quarantine_until: datetime | None = None
    metadata: dict = field(default_factory=dict)


class ProxyManager:
    """Tracks proxy health. Does not bypass site auth, CAPTCHA, or access controls."""

    CIRCUIT_FAILURE_THRESHOLD = 3

    def __init__(
        self,
        *,
        cooldown_s: float = 30.0,
        quarantine_s: float = 300.0,
        circuit_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
    ) -> None:
        self._proxies: dict[str, ProxyRecord] = {}
        self._lock = Lock()
        self._cooldown = timedelta(seconds=cooldown_s)
        self._quarantine = timedelta(seconds=quarantine_s)
        self._circuit_threshold = max(1, circuit_threshold)

    def register(self, proxy_id: str, url: str) -> ProxyRecord:
        with self._lock:
            record = ProxyRecord(proxy_id=proxy_id, url=url)
            self._proxies[proxy_id] = record
            return record

    def acquire(self) -> ProxyRecord | None:
        now = datetime.now(UTC)
        with self._lock:
            self._recover_expired(now)
            for record in self._proxies.values():
                if record.state is ProxyState.HEALTHY:
                    record.state = ProxyState.IN_USE
                    return record
        return None

    def release(
        self,
        proxy_id: str,
        *,
        success: bool,
        error: ProxyErrorType | None = None,
        latency_ms: float | None = None,
    ) -> None:
        with self._lock:
            record = self._proxies.get(proxy_id)
            if record is None:
                return
            now = datetime.now(UTC)
            if latency_ms is not None:
                record.latency_ms = latency_ms

            if success:
                record.successful_requests += 1
                record.consecutive_failures = 0
                record.last_success = now
                record.cooldown_until = None
                record.quarantine_until = None
                record.state = ProxyState.HEALTHY
                return

            record.failed_requests += 1
            record.consecutive_failures += 1
            record.last_failure = now

            if error in {ProxyErrorType.AUTH, ProxyErrorType.PROXY_HANDSHAKE}:
                self._quarantine_now(record, now)
                return

            if record.consecutive_failures >= self._circuit_threshold:
                self._quarantine_now(record, now)
                return

            record.state = ProxyState.COOLING
            record.cooldown_until = now + self._cooldown

    def force_quarantine(self, proxy_id: str, *, reason: str = "") -> None:
        with self._lock:
            record = self._proxies.get(proxy_id)
            if record is None:
                return
            record.metadata["quarantine_reason"] = reason
            self._quarantine_now(record, datetime.now(UTC))

    def get(self, proxy_id: str) -> ProxyRecord | None:
        with self._lock:
            return self._proxies.get(proxy_id)

    def stats(self) -> dict[str, dict]:
        with self._lock:
            self._recover_expired(datetime.now(UTC))
            return {
                pid: {
                    "state": r.state.value,
                    "latency_ms": r.latency_ms,
                    "successful_requests": r.successful_requests,
                    "failed_requests": r.failed_requests,
                    "consecutive_failures": r.consecutive_failures,
                }
                for pid, r in self._proxies.items()
            }

    def healthy_count(self) -> int:
        with self._lock:
            self._recover_expired(datetime.now(UTC))
            return sum(1 for r in self._proxies.values() if r.state is ProxyState.HEALTHY)

    def _quarantine_now(self, record: ProxyRecord, now: datetime) -> None:
        record.state = ProxyState.QUARANTINED
        record.quarantine_until = now + self._quarantine

    def _recover_expired(self, now: datetime) -> None:
        for record in self._proxies.values():
            if (
                record.state is ProxyState.QUARANTINED
                and record.quarantine_until is not None
                and now >= record.quarantine_until
            ):
                record.state = ProxyState.HEALTHY
                record.consecutive_failures = 0
                record.quarantine_until = None
            if (
                record.state is ProxyState.COOLING
                and record.cooldown_until is not None
                and now >= record.cooldown_until
            ):
                record.state = ProxyState.HEALTHY
                record.cooldown_until = None
