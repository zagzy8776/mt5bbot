"""Phase 3 storage tests — OHLC, SQLite async store, historical queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mt5_platform.api import create_app
from mt5_platform.common.enums import OrderSide, OrderStatus, Severity
from mt5_platform.common.events import (
    AccountSnapshot,
    AuditEvent,
    ExecutionRecord,
    MarketDataEvent,
    OrderRequest,
    StrategySignal,
)
from mt5_platform.common.ids import new_execution_id
from mt5_platform.config import Settings, TradingMode, clear_settings_cache
from mt5_platform.storage.db import create_engine, create_session_factory, init_db
from mt5_platform.storage.ohlc import aggregate_ohlc, bucket_start
from mt5_platform.storage.sqlalchemy_store import SqlAlchemyMarketDataStore


def _ticks_around(start: datetime, symbol: str = "XAUUSD") -> list[MarketDataEvent]:
    prices = [2500.0, 2501.0, 2499.5, 2502.0]
    events: list[MarketDataEvent] = []
    for i, price in enumerate(prices):
        events.append(
            MarketDataEvent(
                timestamp=start + timedelta(seconds=i * 10),
                source="test",
                symbol=symbol,
                bid=price - 0.1,
                ask=price + 0.1,
                price=price,
                volume=1.0,
                spread=0.2,
            )
        )
    return events


def test_aggregate_ohlc_1m_candle() -> None:
    start = datetime(2026, 9, 6, 12, 0, 5, tzinfo=UTC)
    events = _ticks_around(start)
    candles = aggregate_ohlc(events, timeframe="1m")
    assert len(candles) == 1
    c = candles[0]
    assert c.symbol == "XAUUSD"
    assert c.timeframe == "1m"
    assert c.open == 2500.0
    assert c.high == 2502.0
    assert c.low == 2499.5
    assert c.close == 2502.0
    assert c.tick_count == 4
    assert c.timestamp == bucket_start(start, "1m")


@pytest.mark.asyncio
async def test_sqlite_store_roundtrip_and_candles() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    store = SqlAlchemyMarketDataStore(create_session_factory(engine))

    start = datetime(2026, 9, 6, 13, 0, 0, tzinfo=UTC)
    events = _ticks_around(start)
    await store.write_ticks(events)

    signal = StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2500.0,
        stop_loss=2490.0,
        take_profit=2520.0,
        confidence=0.6,
        strategy_name="test",
    )
    await store.write_signal(signal)

    order = OrderRequest(
        signal_id=signal.signal_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry=2500.0,
        stop_loss=2490.0,
        take_profit=2520.0,
        status=OrderStatus.APPROVED,
        correlation_id=signal.correlation_id,
    )
    await store.write_order(order)

    execution = ExecutionRecord(
        execution_id=new_execution_id(),
        order_id=order.order_id,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        requested_volume=0.01,
        requested_price=2500.0,
        execution_price=2500.1,
        slippage=0.1,
        final_status=OrderStatus.FILLED,
        correlation_id=order.correlation_id,
    )
    await store.write_execution(execution)

    await store.write_account_snapshot(
        AccountSnapshot(
            balance=10_000,
            equity=10_050,
            free_margin=9_500,
            used_margin=500,
            floating_pnl=50,
            daily_pnl=50,
        )
    )
    await store.write_audit(
        AuditEvent(
            component="storage",
            event_type="DATA_RECEIVED",
            severity=Severity.INFO,
            symbol="XAUUSD",
            payload={"n": 4},
        )
    )
    await store.write_position(
        position_id="pos_1",
        timestamp=start,
        symbol="XAUUSD",
        side="buy",
        volume=0.01,
        entry_price=2500.0,
    )

    ticks = await store.get_ticks(symbol="XAUUSD")
    assert len(ticks) == 4

    candles = await store.build_and_store_candles(symbol="XAUUSD", timeframe="1m")
    assert len(candles) == 1
    stored = await store.get_candles(symbol="XAUUSD", timeframe="1m")
    assert len(stored) == 1
    assert stored[0].close == 2502.0

    assert len(await store.get_signals(symbol="XAUUSD")) == 1
    assert len(await store.get_orders()) == 1
    assert len(await store.get_executions()) == 1
    assert len(await store.get_account_snapshots()) == 1
    assert len(await store.get_audit_events()) == 1
    assert await store.healthcheck() is True

    await engine.dispose()


def test_api_status_reports_phase3_and_storage_backend() -> None:
    clear_settings_cache()
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        storage_backend="memory",
    )
    client = TestClient(create_app(settings))
    status = client.get("/api/v1/status")
    assert status.status_code == 200
    body = status.json()
    assert body["phase"] == 6
    assert body["storage_backend"] == "memory"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["components"]["storage"] == "up"
