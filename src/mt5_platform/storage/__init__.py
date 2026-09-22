"""Storage layer: interfaces, in-memory, and SQLAlchemy backends."""

from __future__ import annotations

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
from mt5_platform.storage.base import MarketDataStore
from mt5_platform.storage.ohlc import Candle, aggregate_ohlc


class InMemoryMarketDataStore(MarketDataStore):
    """In-process store for tests and bootstrapping without a database."""

    def __init__(self) -> None:
        self.ticks: list[MarketDataEvent] = []
        self.signals: list[StrategySignal] = []
        self.orders: list[OrderRequest] = []
        self.executions: list[ExecutionRecord] = []
        self.accounts: list[AccountSnapshot] = []
        self.audits: list[AuditEvent] = []
        self.candles: list[Candle] = []
        self.positions: list[dict] = []
        self.outcomes: list[HistoricalOutcome] = []
        self.news_events: list[NewsEvent] = []
        self.research_notes: list[ResearchNote] = []

    async def write_news_event(self, event: NewsEvent) -> None:
        """Upsert by dedup_key: a re-fetch of the same calendar cannot duplicate a row."""
        for index, existing in enumerate(self.news_events):
            if existing.dedup_key and existing.dedup_key == event.dedup_key:
                self.news_events[index] = event
                return
        self.news_events.append(event)

    async def get_news_events(
        self,
        *,
        currencies: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[NewsEvent]:
        rows = list(self.news_events)
        if currencies:
            rows = [e for e in rows if e.affects(currencies)]
        if since is not None:
            rows = [e for e in rows if e.published_at >= since]
        if until is not None:
            rows = [e for e in rows if e.published_at <= until]
        return sorted(rows, key=lambda e: e.published_at)[:limit]

    async def write_research_note(self, note: ResearchNote) -> None:
        for index, existing in enumerate(self.research_notes):
            if existing.content_hash and existing.content_hash == note.content_hash:
                self.research_notes[index] = note
                return
        self.research_notes.append(note)

    async def get_research_notes(
        self, *, limit: int = 100, since: datetime | None = None
    ) -> list[ResearchNote]:
        rows = list(self.research_notes)
        if since is not None:
            rows = [n for n in rows if n.fetched_at >= since]
        return sorted(rows, key=lambda n: n.fetched_at, reverse=True)[:limit]

    async def write_outcome(self, outcome: HistoricalOutcome) -> None:
        """Upsert by trade_id so reconciliation can never duplicate a record."""
        for index, existing in enumerate(self.outcomes):
            if existing.trade_id == outcome.trade_id:
                self.outcomes[index] = outcome
                return
        self.outcomes.append(outcome)

    async def get_outcomes(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        source: str | None = None,
        symbol: str | None = None,
        strategy: str | None = None,
    ) -> list[HistoricalOutcome]:
        rows = list(self.outcomes)
        if status:
            rows = [o for o in rows if o.status.value == status]
        if source:
            rows = [o for o in rows if o.source.value == source]
        if symbol:
            rows = [o for o in rows if o.instrument.lower() == symbol.lower()]
        if strategy:
            rows = [o for o in rows if o.strategy == strategy]
        return sorted(rows, key=lambda o: o.timestamp, reverse=True)[:limit]

    async def count_outcomes(self, *, status: str | None = None) -> int:
        if status is None:
            return len(self.outcomes)
        return sum(1 for o in self.outcomes if o.status.value == status)

    async def write_tick(self, event: MarketDataEvent) -> None:
        self.ticks.append(event)

    async def write_signal(self, signal: StrategySignal) -> None:
        self.signals.append(signal)

    async def write_order(self, order: OrderRequest) -> None:
        self.orders.append(order)

    async def write_execution(self, execution: ExecutionRecord) -> None:
        self.executions.append(execution)

    async def write_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        self.accounts.append(snapshot)

    async def write_audit(self, event: AuditEvent) -> None:
        self.audits.append(event)

    async def get_signals(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[StrategySignal]:
        rows = self.signals
        if symbol:
            rows = [s for s in rows if s.symbol.lower() == symbol.lower()]
        return sorted(rows, key=lambda s: s.timestamp, reverse=True)[:limit]

    async def write_candles(self, candles: list[Candle]) -> None:
        self.candles.extend(candles)

    async def build_and_store_candles(self, *, symbol: str, timeframe: str = "1m") -> list[Candle]:
        events = [t for t in self.ticks if t.symbol.lower() == symbol.lower()]
        candles = aggregate_ohlc(events, timeframe=timeframe)
        await self.write_candles(candles)
        return candles

    async def get_ticks(
        self,
        *,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 10_000,
    ) -> list[MarketDataEvent]:
        rows = self.ticks
        if symbol:
            rows = [t for t in rows if t.symbol.lower() == symbol.lower()]
        if start:
            rows = [t for t in rows if t.timestamp >= start]
        if end:
            rows = [t for t in rows if t.timestamp <= end]
        return sorted(rows, key=lambda t: t.timestamp)[:limit]

    async def healthcheck(self) -> bool:
        return True


def create_store_from_settings(settings) -> MarketDataStore:
    """Factory: memory | sqlite | postgres (via DATABASE_URL)."""
    backend = getattr(settings, "storage_backend", "memory").lower()
    if backend == "memory":
        return InMemoryMarketDataStore()

    from mt5_platform.storage.db import create_engine, create_session_factory
    from mt5_platform.storage.sqlalchemy_store import SqlAlchemyMarketDataStore

    url = settings.database_url
    if backend == "sqlite" and not str(url).startswith("sqlite"):
        url = "sqlite+aiosqlite:///./mt5_platform.db"
    engine = create_engine(url)
    factory = create_session_factory(engine)
    store = SqlAlchemyMarketDataStore(factory)
    store._engine = engine  # noqa: SLF001 — startup hook attachment
    return store


__all__ = [
    "Candle",
    "InMemoryMarketDataStore",
    "MarketDataStore",
    "aggregate_ohlc",
    "create_store_from_settings",
]
