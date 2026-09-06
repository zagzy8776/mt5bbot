"""Ingestion pipeline: source → validate → sink. Never places trades."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, Severity
from mt5_platform.common.events import AuditEvent, MarketDataEvent
from mt5_platform.ingestion.browser_pool import SharedBrowserPool
from mt5_platform.ingestion.proxy_manager import ProxyManager
from mt5_platform.ingestion.sources import MarketDataSource
from mt5_platform.pipeline import MarketDataValidator, ValidationResult

EventSink = Callable[[MarketDataEvent], Awaitable[None] | None]


@dataclass
class IngestionStats:
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    worker_errors: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)


class IngestionPipeline:
    """High-throughput ingest path isolated from strategy/risk/order layers."""

    def __init__(
        self,
        source: MarketDataSource,
        validator: MarketDataValidator,
        *,
        sink: EventSink | None = None,
        browser_pool: SharedBrowserPool | None = None,
        proxy_manager: ProxyManager | None = None,
        queue_size: int = 1000,
    ) -> None:
        self.source = source
        self.validator = validator
        self.sink = sink
        self.browser_pool = browser_pool
        self.proxy_manager = proxy_manager
        self.stats = IngestionStats()
        self._queue: asyncio.Queue[MarketDataEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._accepted: list[MarketDataEvent] = []

    @property
    def accepted_events(self) -> list[MarketDataEvent]:
        return list(self._accepted)

    async def start(self, *, workers: int = 1) -> None:
        if self._running:
            return
        self._running = True
        await self.source.start()
        if self.browser_pool is not None and not self.browser_pool.is_started:
            await self.browser_pool.start(workers)

        self._tasks = [
            asyncio.create_task(self._produce(), name="ingestion-produce"),
            asyncio.create_task(self._consume(), name="ingestion-consume"),
        ]

    async def stop(self) -> None:
        self._running = False
        await self.source.stop()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.browser_pool is not None and self.browser_pool.is_started:
            await self.browser_pool.stop()

    async def run_until_complete(
        self, *, workers: int = 1, timeout_s: float = 30.0
    ) -> IngestionStats:
        await self.start(workers=workers)
        try:
            await asyncio.wait_for(self._wait_for_idle(), timeout=timeout_s)
        finally:
            await self.stop()
        return self.stats

    async def _wait_for_idle(self) -> None:
        produce = self._tasks[0]
        await produce
        await self._queue.join()

    async def _produce(self) -> None:
        try:
            async for event in self.source.events():
                if not self._running:
                    break
                self.stats.received += 1
                await self._queue.put(event)
                audit_log.emit(
                    AuditEvent(
                        component="ingestion",
                        event_type=AuditEventType.DATA_RECEIVED.value,
                        severity=Severity.DEBUG,
                        symbol=event.symbol,
                        correlation_id=event.correlation_id,
                        payload={"source": event.source},
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.worker_errors += 1
            audit_log.emit(
                AuditEvent(
                    component="ingestion",
                    event_type=AuditEventType.SCRAPER_FAILED.value,
                    severity=Severity.ERROR,
                    error=str(exc),
                )
            )
            # Isolate failure: do not kill consumer; signal end-of-stream.
        finally:
            await self._queue.put(None)

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                result = self.validator.validate(item)
                await self._handle_result(result)
            except Exception:
                self.stats.worker_errors += 1
            finally:
                self._queue.task_done()

    async def _handle_result(self, result: ValidationResult) -> None:
        if not result.accepted or result.event is None:
            self.stats.rejected += 1
            for reason in result.reasons:
                self.stats.reject_reasons[reason] = self.stats.reject_reasons.get(reason, 0) + 1
            return

        self.stats.accepted += 1
        self._accepted.append(result.event)
        if self.sink is not None:
            maybe = self.sink(result.event)
            if maybe is not None and hasattr(maybe, "__await__"):
                await maybe
