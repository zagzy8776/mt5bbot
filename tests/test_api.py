"""API smoke tests for Phase 1 surface."""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from mt5_platform.api import create_app
from mt5_platform.config import Settings, TradingMode, clear_settings_cache


def test_health_and_status_report_demo_mode() -> None:
    clear_settings_cache()
    settings = Settings(trading_mode=TradingMode.DEMO, emergency_kill_switch=False)
    app = create_app(settings)

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            body = health.json()
            assert body["trading_mode"] == "demo"
            assert body["is_live"] is False
            assert "components" in body

            status = await client.get("/api/v1/status")
            assert status.status_code == 200
            assert status.json()["is_demo"] is True
            assert status.json()["phase"] == 6

    asyncio.run(run())
