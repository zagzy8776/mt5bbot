"""Runtime control service tests against the fake MT5 terminal."""

from __future__ import annotations

import asyncio

import pytest

from mt5_platform.config import Settings
from mt5_platform.execution import MockExecutionAdapter
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime import BotControlService
from mt5_platform.signals import SignalEngine
from tests.fake_mt5 import FakeMT5


def test_runtime_service_rejects_mock_backend() -> None:
    settings = Settings(execution_backend="mock")
    service = BotControlService(
        settings=settings,
        adapter=MockExecutionAdapter(),
        signal_engine=SignalEngine([]),
        risk_engine=RiskEngine(settings=settings),
        order_manager=OrderManager(settings=settings),
    )
    with pytest.raises(ValueError, match="EXECUTION_BACKEND=mt5"):
        service._validate_start()


def test_service_wires_intelligence_only_when_enabled() -> None:
    """INTELLIGENCE_ENABLED must activate the layer in the API path (not just the CLI)."""
    from mt5_platform.runtime.intelligence import IntelligenceLayer

    settings = Settings(execution_backend="mt5", intelligence_enabled=True)
    assert settings.intelligence_enabled is True

    fake = FakeMT5()
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    service = BotControlService(
        settings=settings,
        adapter=adapter,
        signal_engine=SignalEngine([]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
    )

    async def run() -> None:
        started = await service.start()
        assert started["state"] == "running"
        assert isinstance(service._loop.intelligence, IntelligenceLayer)
        # intelligence stats must be visible in the snapshot for the dashboard
        snap = service.snapshot
        assert "intelligence" in (snap.get("stats") or {})
        await service.stop()

    asyncio.run(run())


def test_service_has_no_intelligence_layer_by_default() -> None:
    settings = Settings(execution_backend="mt5")
    assert settings.intelligence_enabled is False

    fake = FakeMT5()
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    service = BotControlService(
        settings=settings,
        adapter=adapter,
        signal_engine=SignalEngine([]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
    )

    async def run() -> None:
        await service.start()
        assert service._loop.intelligence is None
        assert "intelligence" not in (service.snapshot.get("stats") or {})
        await service.stop()

    asyncio.run(run())


def test_runtime_service_can_start_and_stop() -> None:
    async def run() -> None:
        fake = FakeMT5()
        settings = Settings(execution_backend="mt5")
        adapter = MT5ExecutionAdapter(settings, client=fake)
        risk = RiskEngine(settings=settings)
        service = BotControlService(
            settings=settings,
            adapter=adapter,
            signal_engine=SignalEngine([]),
            risk_engine=risk,
            order_manager=OrderManager(settings=settings, risk_engine=risk),
            symbol="XAUUSD",
            timeframe="M15",
        )
        started = await service.start()
        assert started["state"] == "running"
        assert started["connected"] is True
        assert started["symbol"] == "XAUUSD"
        stopped = await service.stop()
        assert stopped["state"] == "stopped"
        assert stopped["connected"] is False

    asyncio.run(run())
