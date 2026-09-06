"""Playwright shared browser pool (M1) with optional fake backend for tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from mt5_platform.ingestion.context_config import ContextConfig


class BrowserBackend(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def new_context(self, config: ContextConfig, proxy_url: str | None) -> Any: ...

    async def close_context(self, context: Any) -> None: ...


@dataclass
class PooledContext:
    context_id: str
    context: Any
    config: ContextConfig
    proxy_id: str | None = None
    proxy_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    stale: bool = False


class FakeBrowserBackend:
    """In-memory backend so Phase 2 logic is testable without Chromium."""

    def __init__(self) -> None:
        self.started = False
        self.contexts_created = 0
        self.contexts_closed = 0
        self._open: set[str] = set()

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False
        self._open.clear()

    async def new_context(self, config: ContextConfig, proxy_url: str | None) -> dict[str, Any]:
        if not self.started:
            raise RuntimeError("FakeBrowserBackend is not started")
        self.contexts_created += 1
        cid = f"fake-{self.contexts_created}"
        self._open.add(cid)
        return {
            "id": cid,
            "config": config,
            "proxy_url": proxy_url,
            "pages": [],
        }

    async def close_context(self, context: Any) -> None:
        cid = context.get("id") if isinstance(context, dict) else None
        if cid in self._open:
            self._open.remove(cid)
            self.contexts_closed += 1


class PlaywrightBrowserBackend:
    """Shared Chromium process via Playwright async API."""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._playwright: Any = None
        self._browser: Any = None

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "Playwright is not installed. Install with: pip install 'mt5-platform[ingestion]'"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def new_context(self, config: ContextConfig, proxy_url: str | None) -> Any:
        if self._browser is None:
            raise RuntimeError("Playwright browser is not started")
        options = config.to_playwright_options()
        if proxy_url:
            options["proxy"] = {"server": proxy_url}
        return await self._browser.new_context(**options)

    async def close_context(self, context: Any) -> None:
        await context.close()


class SharedBrowserPool:
    """One browser process, many isolated contexts, semaphore-bounded workers."""

    def __init__(
        self,
        *,
        backend: BrowserBackend | None = None,
        stale_after_s: float = 300.0,
        default_config: ContextConfig | None = None,
    ) -> None:
        self._backend = backend or FakeBrowserBackend()
        self._stale_after_s = stale_after_s
        self._default_config = default_config or ContextConfig()
        self._workers = 0
        self._semaphore: asyncio.Semaphore | None = None
        self._started = False
        self._stopping = False
        self._active: dict[str, PooledContext] = {}
        self._lock = asyncio.Lock()
        self.worker_failures = 0

    @property
    def is_started(self) -> bool:
        return self._started and not self._stopping

    @property
    def workers(self) -> int:
        return self._workers

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def start(self, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if self._started:
            return
        self._workers = workers
        self._semaphore = asyncio.Semaphore(workers)
        self._stopping = False
        await self._backend.start()
        self._started = True

    async def stop(self) -> None:
        self._stopping = True
        async with self._lock:
            for pooled in list(self._active.values()):
                await self._safe_close(pooled)
            self._active.clear()
        await self._backend.stop()
        self._started = False
        self._semaphore = None
        self._stopping = False

    async def acquire_context(
        self,
        *,
        config: ContextConfig | None = None,
        proxy_id: str | None = None,
        proxy_url: str | None = None,
    ) -> PooledContext:
        if not self.is_started or self._semaphore is None:
            raise RuntimeError("BrowserPool is not started")

        await self._semaphore.acquire()
        try:
            cfg = config or self._default_config
            context = await self._backend.new_context(cfg, proxy_url)
            pooled = PooledContext(
                context_id=str(uuid4()),
                context=context,
                config=cfg,
                proxy_id=proxy_id,
                proxy_url=proxy_url,
            )
            async with self._lock:
                self._active[pooled.context_id] = pooled
            return pooled
        except Exception:
            self._semaphore.release()
            self.worker_failures += 1
            raise

    async def release_context(self, pooled: PooledContext, *, recycle: bool = False) -> None:
        async with self._lock:
            self._active.pop(pooled.context_id, None)
        await self._safe_close(pooled)
        if recycle:
            # Caller may re-acquire; slot is always released below.
            pass
        if self._semaphore is not None:
            self._semaphore.release()

    async def mark_stale_and_recycle(self, pooled: PooledContext) -> PooledContext:
        """Close a stale session and open a replacement under the same worker slot."""
        pooled.stale = True
        cfg = pooled.config
        proxy_id = pooled.proxy_id
        proxy_url = pooled.proxy_url
        async with self._lock:
            self._active.pop(pooled.context_id, None)
        await self._safe_close(pooled)

        context = await self._backend.new_context(cfg, proxy_url)
        replacement = PooledContext(
            context_id=str(uuid4()),
            context=context,
            config=cfg,
            proxy_id=proxy_id,
            proxy_url=proxy_url,
        )
        async with self._lock:
            self._active[replacement.context_id] = replacement
        return replacement

    def is_context_stale(self, pooled: PooledContext, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        age = (now - pooled.created_at).total_seconds()
        idle = (now - pooled.last_used_at).total_seconds()
        return pooled.stale or age >= self._stale_after_s or idle >= self._stale_after_s

    def touch(self, pooled: PooledContext) -> None:
        pooled.last_used_at = datetime.now(UTC)

    async def _safe_close(self, pooled: PooledContext) -> None:
        try:
            await self._backend.close_context(pooled.context)
        except Exception:
            self.worker_failures += 1
