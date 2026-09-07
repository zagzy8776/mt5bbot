"""Async SQLAlchemy market-data / trading store."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mt5_platform.common.enums import OrderSide, OrderStatus, Severity
from mt5_platform.common.events import (
    AccountSnapshot,
    AuditEvent,
    ExecutionRecord,
    MarketDataEvent,
    OrderRequest,
    StrategySignal,
)
from mt5_platform.storage.base import MarketDataStore
from mt5_platform.storage.models import (
    AccountSnapshotRow,
    AuditRow,
    CandleRow,
    ExecutionRow,
    OrderRow,
    PositionRow,
    SignalRow,
    TickRow,
)
from mt5_platform.storage.ohlc import Candle, aggregate_ohlc


class SqlAlchemyMarketDataStore(MarketDataStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def write_tick(self, event: MarketDataEvent) -> None:
        async with self._session_factory() as session:
            session.add(
                TickRow(
                    timestamp=event.timestamp,
                    source=event.source,
                    symbol=event.symbol,
                    bid=event.bid,
                    ask=event.ask,
                    price=event.price,
                    volume=event.volume,
                    spread=event.spread,
                    metadata_json=event.metadata,
                    correlation_id=event.correlation_id,
                )
            )
            await session.commit()

    async def write_ticks(self, events: list[MarketDataEvent]) -> None:
        if not events:
            return
        async with self._session_factory() as session:
            for event in events:
                session.add(
                    TickRow(
                        timestamp=event.timestamp,
                        source=event.source,
                        symbol=event.symbol,
                        bid=event.bid,
                        ask=event.ask,
                        price=event.price,
                        volume=event.volume,
                        spread=event.spread,
                        metadata_json=event.metadata,
                        correlation_id=event.correlation_id,
                    )
                )
            await session.commit()

    async def write_signal(self, signal: StrategySignal) -> None:
        async with self._session_factory() as session:
            session.add(
                SignalRow(
                    signal_id=signal.signal_id,
                    timestamp=signal.timestamp,
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    entry=signal.entry,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    confidence=signal.confidence,
                    reason=signal.reason,
                    strategy_name=signal.strategy_name,
                    metadata_json=signal.metadata,
                    correlation_id=signal.correlation_id,
                )
            )
            await session.commit()

    async def write_order(self, order: OrderRequest) -> None:
        async with self._session_factory() as session:
            session.add(
                OrderRow(
                    order_id=order.order_id,
                    signal_id=order.signal_id,
                    created_at=order.created_at,
                    symbol=order.symbol,
                    side=order.side.value,
                    volume=order.volume,
                    entry=order.entry,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    status=order.status.value,
                    metadata_json=order.metadata,
                    correlation_id=order.correlation_id,
                )
            )
            await session.commit()

    async def write_execution(self, execution: ExecutionRecord) -> None:
        async with self._session_factory() as session:
            session.add(
                ExecutionRow(
                    execution_id=execution.execution_id,
                    order_id=execution.order_id,
                    timestamp=execution.timestamp,
                    symbol=execution.symbol,
                    side=execution.side.value,
                    requested_volume=execution.requested_volume,
                    requested_price=execution.requested_price,
                    stop_loss=execution.stop_loss,
                    take_profit=execution.take_profit,
                    mt5_response=execution.mt5_response,
                    execution_price=execution.execution_price,
                    slippage=execution.slippage,
                    rejection_reason=execution.rejection_reason,
                    final_status=execution.final_status.value,
                    correlation_id=execution.correlation_id,
                )
            )
            await session.commit()

    async def write_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        async with self._session_factory() as session:
            session.add(
                AccountSnapshotRow(
                    timestamp=snapshot.timestamp,
                    balance=snapshot.balance,
                    equity=snapshot.equity,
                    free_margin=snapshot.free_margin,
                    used_margin=snapshot.used_margin,
                    margin_level=snapshot.margin_level,
                    floating_pnl=snapshot.floating_pnl,
                    daily_pnl=snapshot.daily_pnl,
                    drawdown_pct=snapshot.drawdown_pct,
                    open_positions=snapshot.open_positions,
                    exposure=snapshot.exposure,
                )
            )
            await session.commit()

    async def write_audit(self, event: AuditEvent) -> None:
        async with self._session_factory() as session:
            session.add(
                AuditRow(
                    timestamp=event.timestamp,
                    component=event.component,
                    event_type=event.event_type,
                    severity=event.severity.value,
                    symbol=event.symbol,
                    correlation_id=event.correlation_id,
                    payload=event.payload,
                    error=event.error,
                )
            )
            await session.commit()

    async def write_position(
        self,
        *,
        position_id: str,
        timestamp: datetime,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        unrealized_pnl: float = 0.0,
        status: str = "open",
        metadata: dict | None = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                PositionRow(
                    position_id=position_id,
                    timestamp=timestamp,
                    symbol=symbol.upper(),
                    side=side,
                    volume=volume,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    unrealized_pnl=unrealized_pnl,
                    status=status,
                    metadata_json=metadata or {},
                )
            )
            await session.commit()

    async def write_candles(self, candles: list[Candle]) -> None:
        if not candles:
            return
        async with self._session_factory() as session:
            for candle in candles:
                session.add(
                    CandleRow(
                        timestamp=candle.timestamp,
                        symbol=candle.symbol,
                        timeframe=candle.timeframe,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                        tick_count=candle.tick_count,
                        spread_avg=candle.spread_avg,
                    )
                )
            await session.commit()

    async def build_and_store_candles(
        self,
        *,
        symbol: str,
        timeframe: str = "1m",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        ticks = await self.get_ticks(symbol=symbol, start=start, end=end)
        candles = aggregate_ohlc(ticks, timeframe=timeframe)
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
        stmt: Select[tuple[TickRow]] = (
            select(TickRow).order_by(TickRow.timestamp.asc()).limit(limit)
        )
        if symbol:
            stmt = stmt.where(TickRow.symbol == symbol.upper())
        if start:
            stmt = stmt.where(TickRow.timestamp >= start)
        if end:
            stmt = stmt.where(TickRow.timestamp <= end)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            MarketDataEvent(
                timestamp=row.timestamp,
                source=row.source,
                symbol=row.symbol,
                bid=row.bid,
                ask=row.ask,
                price=row.price,
                volume=row.volume,
                spread=row.spread,
                metadata=row.metadata_json or {},
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]

    async def get_candles(
        self,
        *,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 1000,
    ) -> list[Candle]:
        stmt = (
            select(CandleRow)
            .where(CandleRow.symbol == symbol.upper(), CandleRow.timeframe == timeframe)
            .order_by(CandleRow.timestamp.asc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            Candle(
                timestamp=row.timestamp,
                symbol=row.symbol,
                timeframe=row.timeframe,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                tick_count=row.tick_count,
                spread_avg=row.spread_avg,
            )
            for row in rows
        ]

    async def get_signals(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> list[StrategySignal]:
        stmt = select(SignalRow).order_by(SignalRow.timestamp.desc()).limit(limit)
        if symbol:
            stmt = stmt.where(SignalRow.symbol == symbol.upper())
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            StrategySignal(
                signal_id=row.signal_id,
                timestamp=row.timestamp,
                symbol=row.symbol,
                direction=OrderSide(row.direction),
                entry=row.entry,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
                confidence=row.confidence,
                reason=row.reason,
                strategy_name=row.strategy_name,
                metadata=row.metadata_json or {},
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]

    async def get_orders(self, *, limit: int = 100) -> list[OrderRequest]:
        stmt = select(OrderRow).order_by(OrderRow.created_at.desc()).limit(limit)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            OrderRequest(
                order_id=row.order_id,
                signal_id=row.signal_id,
                created_at=row.created_at,
                symbol=row.symbol,
                side=OrderSide(row.side),
                volume=row.volume,
                entry=row.entry,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
                status=OrderStatus(row.status),
                metadata=row.metadata_json or {},
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]

    async def get_executions(self, *, limit: int = 100) -> list[ExecutionRecord]:
        stmt = select(ExecutionRow).order_by(ExecutionRow.timestamp.desc()).limit(limit)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            ExecutionRecord(
                execution_id=row.execution_id,
                order_id=row.order_id,
                timestamp=row.timestamp,
                symbol=row.symbol,
                side=OrderSide(row.side),
                requested_volume=row.requested_volume,
                requested_price=row.requested_price,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
                mt5_response=row.mt5_response or {},
                execution_price=row.execution_price,
                slippage=row.slippage,
                rejection_reason=row.rejection_reason,
                final_status=OrderStatus(row.final_status),
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]

    async def get_account_snapshots(self, *, limit: int = 100) -> list[AccountSnapshot]:
        stmt = select(AccountSnapshotRow).order_by(AccountSnapshotRow.timestamp.desc()).limit(limit)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            AccountSnapshot(
                timestamp=row.timestamp,
                balance=row.balance,
                equity=row.equity,
                free_margin=row.free_margin,
                used_margin=row.used_margin,
                margin_level=row.margin_level,
                floating_pnl=row.floating_pnl,
                daily_pnl=row.daily_pnl,
                drawdown_pct=row.drawdown_pct,
                open_positions=row.open_positions,
                exposure=row.exposure,
            )
            for row in rows
        ]

    async def get_audit_events(self, *, limit: int = 100) -> list[AuditEvent]:
        stmt = select(AuditRow).order_by(AuditRow.timestamp.desc()).limit(limit)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            AuditEvent(
                timestamp=row.timestamp,
                component=row.component,
                event_type=row.event_type,
                severity=Severity(row.severity),
                symbol=row.symbol,
                correlation_id=row.correlation_id,
                payload=row.payload or {},
                error=row.error,
            )
            for row in rows
        ]

    async def healthcheck(self) -> bool:
        async with self._session_factory() as session:
            await session.execute(select(1))
        return True
