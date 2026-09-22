"""Storage abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from mt5_platform.common.events import (
    AccountSnapshot,
    AuditEvent,
    ExecutionRecord,
    MarketDataEvent,
    NewsEvent,
    OrderRequest,
    ResearchNote,
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

    # --------------------------------------------------- news events / research notes

    async def write_news_event(self, event: NewsEvent) -> None:
        """Persist one macro event (idempotent by dedup_key)."""
        raise NotImplementedError

    async def get_news_events(
        self,
        *,
        currencies: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[NewsEvent]:
        raise NotImplementedError

    async def write_research_note(self, note: ResearchNote) -> None:
        """Persist one research finding (idempotent by content hash)."""
        raise NotImplementedError

    async def get_research_notes(
        self, *, limit: int = 100, since: datetime | None = None
    ) -> list[ResearchNote]:
        raise NotImplementedError
