"""Storage abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from mt5_platform.common.events import (
    AccountSnapshot,
    AuditEvent,
    ExecutionRecord,
    MarketDataEvent,
    OrderRequest,
    StrategySignal,
)


class MarketDataStore(ABC):
    @abstractmethod
    async def write_tick(self, event: MarketDataEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def write_signal(self, signal: StrategySignal) -> None:
        raise NotImplementedError

    @abstractmethod
    async def write_order(self, order: OrderRequest) -> None:
        raise NotImplementedError

    @abstractmethod
    async def write_execution(self, execution: ExecutionRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    async def write_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        raise NotImplementedError

    @abstractmethod
    async def write_audit(self, event: AuditEvent) -> None:
        raise NotImplementedError
