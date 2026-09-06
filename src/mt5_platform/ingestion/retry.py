"""Retry helpers with exponential backoff (ingestion-safe)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_delay_s: float = 0.25,
    max_delay_s: float = 8.0,
    jitter: float = 0.2,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run ``fn`` with exponential backoff. Does not swallow the final failure."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:  # noqa: PERF203 — intentional retry loop
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            if jitter > 0:
                delay *= 1.0 + random.uniform(-jitter, jitter)
            delay = max(0.0, delay)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc
