"""Market data sources. Sources emit events only — never place trades."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from mt5_platform.common.events import MarketDataEvent
from mt5_platform.ingestion.retry import retry_async


class MarketDataSource(ABC):
    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def events(self) -> AsyncIterator[MarketDataEvent]:
        raise NotImplementedError


class SyntheticQuoteSource(MarketDataSource):
    """Deterministic quote generator for pipeline tests and local dry-runs."""

    def __init__(
        self,
        *,
        symbol: str = "XAUUSD",
        source: str = "synthetic",
        interval_s: float = 0.01,
        quotes: list[dict[str, Any]] | None = None,
        max_events: int | None = 5,
    ) -> None:
        self.symbol = symbol.upper()
        self.source_name = source
        self.interval_s = interval_s
        self.max_events = max_events
        self._quotes = quotes or [
            {"bid": 2500.0, "ask": 2500.5, "price": 2500.25, "volume": 1.0},
            {"bid": 2500.1, "ask": 2500.6, "price": 2500.35, "volume": 1.0},
            {"bid": 2500.2, "ask": 2500.7, "price": 2500.45, "volume": 2.0},
            {"bid": 2500.15, "ask": 2500.65, "price": 2500.40, "volume": 1.0},
            {"bid": 2500.3, "ask": 2500.8, "price": 2500.55, "volume": 1.0},
        ]
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def events(self) -> AsyncIterator[MarketDataEvent]:
        emitted = 0
        idx = 0
        while self._running:
            if self.max_events is not None and emitted >= self.max_events:
                break
            q = self._quotes[idx % len(self._quotes)]
            bid = float(q["bid"])
            ask = float(q["ask"])
            yield MarketDataEvent(
                timestamp=datetime.now(UTC),
                source=self.source_name,
                symbol=self.symbol,
                bid=bid,
                ask=ask,
                price=float(q.get("price", (bid + ask) / 2.0)),
                volume=float(q.get("volume", 0.0)),
                spread=ask - bid,
                metadata={"synthetic": True, "index": idx},
            )
            emitted += 1
            idx += 1
            await asyncio.sleep(self.interval_s)


class CallableQuoteSource(MarketDataSource):
    """Wraps an async fetch callback (authorized API) with retry/backoff."""

    def __init__(
        self,
        fetch: Callable[[], Any],
        *,
        source: str = "authorized_api",
        interval_s: float = 1.0,
        max_events: int | None = None,
        retry_attempts: int = 3,
    ) -> None:
        self._fetch = fetch
        self.source_name = source
        self.interval_s = interval_s
        self.max_events = max_events
        self.retry_attempts = retry_attempts
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def events(self) -> AsyncIterator[MarketDataEvent]:
        emitted = 0
        while self._running:
            if self.max_events is not None and emitted >= self.max_events:
                break

            async def _once() -> MarketDataEvent:
                result = self._fetch()
                if asyncio.iscoroutine(result):
                    result = await result
                if not isinstance(result, MarketDataEvent):
                    raise TypeError("fetch must return MarketDataEvent")
                return result

            event = await retry_async(_once, attempts=self.retry_attempts)
            yield event
            emitted += 1
            await asyncio.sleep(self.interval_s)
