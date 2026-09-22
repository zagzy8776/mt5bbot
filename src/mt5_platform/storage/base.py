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
from mt5_platform.historical.models import HistoricalOutcome


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

    # ------------------------------------------------------------- outcome ledger
    # Concrete (not abstract) so existing stores keep working; the recorder detects capability
    # instead of assuming it, and degrades to in-memory without touching the trading path.

    async def write_outcome(self, outcome: HistoricalOutcome) -> None:
        """Persist one outcome (idempotent by trade_id) plus its legs."""
        raise NotImplementedError

    async def get_outcomes(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        source: str | None = None,
        symbol: str | None = None,
        strategy: str | None = None,
    ) -> list[HistoricalOutcome]:
        raise NotImplementedError

    async def get_open_outcomes(self) -> list[HistoricalOutcome]:
        """Outcomes still tracking an open position (used to resume after a restart)."""
        return await self.get_outcomes(limit=1000, status="open")

    async def count_outcomes(self, *, status: str | None = None) -> int:
        raise NotImplementedError
