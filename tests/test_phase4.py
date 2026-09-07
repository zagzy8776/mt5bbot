"""Phase 4 strategy / signal-engine tests — interface, engine, sinks, API surface."""

from __future__ import annotations

import asyncio
import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from mt5_platform.api import create_app
from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent
from mt5_platform.config import Settings, TradingMode, clear_settings_cache
from mt5_platform.signals import (
    AuditStoreSink,
    SignalEngine,
    SignalStoreSink,
    build_signal_engine,
)
from mt5_platform.storage import InMemoryMarketDataStore, create_store_from_settings
from mt5_platform.storage.db import init_db
from mt5_platform.strategy import (
    BreakoutStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    SmaCrossoverStrategy,
    available_strategies,
    create_strategy,
    describe_available,
    mid_price,
)


def _tick(
    price: float,
    ts: datetime | None = None,
    symbol: str = "XAUUSD",
) -> MarketDataEvent:
    return MarketDataEvent(
        timestamp=ts or datetime.now(UTC),
        source="test",
        symbol=symbol,
        price=price,
        bid=price - 0.1,
        ask=price + 0.1,
    )


def test_mid_price_prefers_price_then_bid_ask_midpoint() -> None:
    base = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    full = MarketDataEvent(timestamp=base, source="t", symbol="X", bid=10.0, ask=12.0, price=11.5)
    assert mid_price(full) == 11.5

    spreads = MarketDataEvent(timestamp=base, source="t", symbol="X", bid=10.0, ask=12.0)
    assert mid_price(spreads) == 11.0

    one_sided = MarketDataEvent(timestamp=base, source="t", symbol="X", bid=None, ask=12.0)
    assert mid_price(one_sided) == 12.0


def test_strategy_info_describes_parameters() -> None:
    strategy = SmaCrossoverStrategy(
        fast_period=3,
        slow_period=10,
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
        symbols={"xauusd"},
    )
    info = strategy.info()
    assert info["name"] == "sma_crossover"
    assert info["enabled"] is True
    assert info["symbols"] == ["XAUUSD"]
    assert info["parameters"]["fast_period"] == 3
    assert info["parameters"]["slow_period"] == 10
    assert info["parameters"]["stop_loss_pct"] == 0.5
    assert info["version"]
    assert info["description"]
    assert strategy.handles("XAUUSD")
    assert not strategy.handles("EURUSD")


def test_pct_risk_mixin_levels() -> None:
    strategy = MomentumStrategy(stop_loss_pct=0.5, take_profit_pct=1.0)
    event = _tick(100.0)
    assert strategy.calculate_entry(event, OrderSide.BUY) == 100.0
    assert strategy.calculate_stop_loss(100.0, OrderSide.BUY) == pytest.approx(99.5)
    assert strategy.calculate_take_profit(100.0, OrderSide.BUY) == pytest.approx(101.0)
    assert strategy.calculate_stop_loss(100.0, OrderSide.SELL) == pytest.approx(100.5)
    assert strategy.calculate_take_profit(100.0, OrderSide.SELL) == pytest.approx(99.0)


def test_registry_offers_all_strategies() -> None:
    expected = ["breakout", "mean_reversion", "momentum", "null", "sma_crossover"]
    assert available_strategies() == expected
    names = {entry["name"] for entry in describe_available()}
    assert names == set(available_strategies())
    with pytest.raises(ValueError):
        create_strategy("not_a_strategy")


def test_mean_reversion_buys_on_zscore_dip() -> None:
    strategy = MeanReversionStrategy(window=5, threshold=2.0)
    start = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    for i in range(5):
        assert strategy.generate_signal(_tick(100.0, start + timedelta(seconds=i))) is None

    signal = strategy.generate_signal(_tick(95.0, start + timedelta(seconds=5)))
    assert signal is not None
    assert signal.direction is OrderSide.BUY
    assert signal.entry == 95.0
    assert signal.stop_loss is not None and signal.stop_loss < signal.entry
    assert signal.take_profit is not None and signal.take_profit > signal.entry
    assert 0.0 < signal.confidence <= 1.0
    assert signal.reason.startswith("mean_reversion_buy")


def test_momentum_fires_on_directional_move_once() -> None:
    strategy = MomentumStrategy(lookback=5, threshold_pct=1.0)
    start = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    for i in range(5):
        assert strategy.generate_signal(_tick(100.0, start + timedelta(seconds=i))) is None

    buy = strategy.generate_signal(_tick(102.0, start + timedelta(seconds=5)))
    assert buy is not None and buy.direction is OrderSide.BUY

    # Staying above the threshold must not re-fire.
    assert strategy.generate_signal(_tick(103.0, start + timedelta(seconds=6))) is None


def test_sma_crossover_still_crosses_after_refactor() -> None:
    strategy = SmaCrossoverStrategy(fast_period=2, slow_period=4)
    start = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    prices = [10.0, 10.0, 10.0, 10.0, 11.0, 12.0, 13.0, 10.0]
    signals = [
        strategy.generate_signal(_tick(price, start + timedelta(seconds=i)))
        for i, price in enumerate(prices)
    ]
    ups = [s for s in signals if s is not None and s.direction is OrderSide.BUY]
    downs = [s for s in signals if s is not None and s.direction is OrderSide.SELL]
    assert ups, "expected an upside SMA cross signal"
    assert downs, "expected a downside SMA cross signal"
@pytest.mark.asyncio
async def test_engine_persists_signals_and_audits_to_in_memory_store() -> None:
    store = InMemoryMarketDataStore()
    engine = SignalEngine(
        [BreakoutStrategy(lookback=2), SmaCrossoverStrategy(fast_period=1, slow_period=2)],
        sink=SignalStoreSink(store),
        audit_sink=AuditStoreSink(store),
    )
    start = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    for i, price in enumerate([100.0, 101.0, 105.0]):
        await engine.on_market_data(_tick(price, start + timedelta(seconds=i)))

    assert engine.stats.events_processed == 3
    assert engine.stats.signals_generated >= 1
    assert len(store.signals) == engine.stats.signals_generated
    assert any(e.event_type == "SIGNAL_GENERATED" for e in store.audits)

    snapshot = engine.stats_snapshot()
    assert snapshot["strategy_stats"]["breakout"]["signals_generated"] >= 1
    assert snapshot["total_strategies"] == 2
    assert snapshot["active_strategies"] == 2
    assert len(engine.recent_signals(limit=10)) == engine.stats.signals_generated


@pytest.mark.asyncio
async def test_engine_rejects_low_confidence_signals() -> None:
    engine = SignalEngine([BreakoutStrategy(lookback=2)], min_confidence=1.0)
    start = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    for i, price in enumerate([100.0, 101.0, 105.0]):
        await engine.on_market_data(_tick(price, start + timedelta(seconds=i)))

    assert engine.stats.signals_generated == 0
    assert engine.stats.signals_rejected == 1
    assert engine.stats.reject_reasons.get("confidence_below_minimum") == 1
    assert engine.stats_snapshot()["strategy_stats"]["breakout"]["signals_rejected"] == 1


@pytest.mark.asyncio
async def test_engine_cooldown_suppresses_spam() -> None:
    engine = SignalEngine([BreakoutStrategy(lookback=2)], cooldown_s=60.0)
    start = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    for i, price in enumerate([100.0, 101.0, 105.0, 106.0]):
        await engine.on_market_data(_tick(price, start + timedelta(seconds=i)))

    assert engine.stats.signals_generated == 1
    assert engine.stats.signals_rejected == 1
    assert engine.stats.reject_reasons.get("cooldown") == 1


@pytest.mark.asyncio
async def test_engine_enable_disable_runtime_control() -> None:
    engine = SignalEngine([BreakoutStrategy(lookback=2), create_strategy("null")])
    start = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)

    assert (await engine.disable("breakout")).enabled is False
    for i, price in enumerate([100.0, 101.0, 105.0]):
        await engine.on_market_data(_tick(price, start + timedelta(seconds=i)))
    assert engine.stats.signals_generated == 0

    assert (await engine.enable("breakout")).enabled is True
    assert await engine.disable("missing") is None
    assert engine.get_strategy("null") is not None


@pytest.mark.asyncio
async def test_build_signal_engine_persists_to_sqlite_store() -> None:
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        storage_backend="sqlite",
        database_url="sqlite+aiosqlite:///:memory:",
        strategies="breakout",
    )
    store = create_store_from_settings(settings)
    await init_db(store._engine)  # noqa: SLF001
    engine = build_signal_engine(settings, store)

    start = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    # Default breakout requires 20 warmup ticks, then a price above the window high.
    prices = [2500.0] * 20 + [2600.0]
    for i, price in enumerate(prices):
        await engine.on_market_data(_tick(price, start + timedelta(seconds=i)))

    rows = await store.get_signals(symbol="XAUUSD")
    assert len(rows) == 1
    assert rows[0].direction is OrderSide.BUY
    assert len(await store.get_audit_events(limit=50)) >= 1
    await store._engine.dispose()  # noqa: SLF001
def test_build_signal_engine_skips_unknown_strategies() -> None:
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        storage_backend="memory",
        strategies="breakout,bogus_strategy",
        signal_store_sink_enabled=False,
        signal_audit_store_sink_enabled=False,
    )
    engine = build_signal_engine(settings, InMemoryMarketDataStore())
    assert [s.name for s in engine.strategies] == ["breakout"]


def test_api_strategy_and_signal_surface() -> None:
    clear_settings_cache()
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        storage_backend="memory",
        strategies="sma_crossover,breakout",
        signal_min_confidence=0.0,
        signal_require_stop_loss=True,
    )
    app = create_app(settings)

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = await client.get("/api/v1/status")
            assert status.status_code == 200
            body = status.json()
            assert body["phase"] == 6
            assert body["strategies"] == ["sma_crossover", "breakout"]

            listed = (await client.get("/api/v1/strategies")).json()["strategies"]
            assert {s["name"] for s in listed} == {"sma_crossover", "breakout"}
            assert listed[0]["stats"]["events_processed"] == 0

            available = (await client.get("/api/v1/strategies/available")).json()["available"]
            assert {s["name"] for s in available} == set(available_strategies())

            assert (await client.post("/api/v1/strategies/breakout/disable")).json()["enabled"] is False
            assert (await client.post("/api/v1/strategies/breakout/enable")).json()["enabled"] is True
            assert (await client.post("/api/v1/strategies/nope/enable")).status_code == 404

            start = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
            # Default breakout needs 20 warmup ticks, then a break above the window high.
            prices = [2500.0] * 20 + [2600.0]
            for i, price in enumerate(prices):
                resp = await client.post(
                    "/api/v1/signals/evaluate",
                    json={
                        "timestamp": (start + timedelta(seconds=i)).isoformat(),
                        "source": "test",
                        "symbol": "XAUUSD",
                        "price": price,
                    },
                )
                assert resp.status_code == 200

            stats = (await client.get("/api/v1/signals/stats")).json()
            assert stats["events_processed"] == len(prices)
            assert stats["signals_generated"] >= 1
            signals = (await client.get("/api/v1/signals")).json()["signals"]
            assert len(signals) >= 1
    asyncio.run(run())


def test_strategy_and_signals_do_not_import_trading_layers() -> None:
    """Architectural guard: strategy/signal code must stay out of execution paths."""
    root = Path(__file__).resolve().parents[1] / "src" / "mt5_platform"
    forbidden = ("mt5_platform.orders", "mt5_platform.execution", "mt5_platform.risk")
    for package in ("strategy", "signals"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not any(node.module.startswith(f) for f in forbidden), path
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not any(alias.name.startswith(f) for f in forbidden), path