"""API smoke tests for Phase 1 surface."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from mt5_platform.api import create_app
from mt5_platform.common.enums import OrderSide, OutcomeSource
from mt5_platform.config import Settings, TradingMode, clear_settings_cache
from mt5_platform.historical.models import HistoricalOutcome


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
            assert status.json()["runtime"]["state"] == "stopped"

            runtime = await client.get("/api/v1/runtime")
            assert runtime.status_code == 200
            assert runtime.json()["state"] == "stopped"

            start = await client.post(
                "/api/v1/runtime/start",
                json={"symbol": "XAUUSD", "timeframe": "M15"},
            )
            assert start.status_code == 409

    asyncio.run(run())


def _outcome(index: int, *, source: OutcomeSource = OutcomeSource.AUTONOMOUS) -> HistoricalOutcome:
    start = datetime(2026, 9, 22, 9, 0, tzinfo=UTC) + timedelta(minutes=index)
    return HistoricalOutcome(
        instrument="XAUUSD",
        direction=OrderSide.BUY,
        source=source,
        strategy="breakout",
        timeframe="M15",
        timestamp=start,
        entry=4300.0,
        exit_price=4310.0,
        exit_time=start + timedelta(minutes=30),
        realized_pnl=10.0,
        return_pct=10.0,
    )


def test_outcomes_endpoint_reports_records_and_evidence_quality() -> None:
    """The dashboard must see recorded outcomes, and evidence quality must stay honest."""
    clear_settings_cache()
    settings = Settings(trading_mode=TradingMode.DEMO, emergency_kill_switch=False)
    app = create_app(settings)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            store = app.state.store
            await store.write_outcome(_outcome(1))
            await store.write_outcome(_outcome(2, source=OutcomeSource.EXTERNAL))
            await store.write_outcome(_outcome(3, source=OutcomeSource.BACKTEST))

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/outcomes?limit=10")
                assert response.status_code == 200
                body = response.json()
                assert body["count"] == 3
                assert "summary" in body and "learning" in body

                evidence = body["evidence"]
                assert evidence["sample_size"] == 2  # backtest outcomes are excluded
                assert evidence["sources"] == {"autonomous": 1, "external": 1}
                assert evidence["backtest_excluded"] is True
                assert evidence["evidence_quality"] == "insufficient"  # 2 < 10, not rounded up
                assert evidence["minimum_sample_required"] == 10
                assert (evidence["min_sample_moderate"], evidence["min_sample_strong"]) == (30, 100)
                assert evidence["thresholds_lowered"] is False

                sources = {row["source"] for row in body["outcomes"]}
                assert sources == {"autonomous", "external", "backtest"}

                assert (await client.get("/api/v1/outcomes?status=open")).json()["count"] == 0
                assert (await client.get("/api/v1/outcomes?status=closed")).json()["count"] == 3

    asyncio.run(run())


def test_outcomes_endpoint_is_exposed_in_runtime_stats_shape() -> None:
    """The runtime snapshot carries the outcome/learning/evidence blocks for the dashboard."""
    clear_settings_cache()
    settings = Settings(trading_mode=TradingMode.DEMO, emergency_kill_switch=False)
    app = create_app(settings)
    service = app.state.runtime_service
    snapshot = service.snapshot
    stats = snapshot["stats"]
    assert "outcomes" in stats and "learning" in stats and "evidence" in stats
    assert stats["evidence"]["minimum_sample_required"] == 10
    assert stats["evidence"]["thresholds_lowered"] is False
