"""Strategy panel: research verdicts, guarded switches, and the effective-state payload."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from mt5_platform.api import create_app
from mt5_platform.config import Settings, TradingMode


def _report(tmp_path: Path) -> str:
    path = tmp_path / "research_report.json"
    path.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "name": "Scalp MB 20",
                        "strategy": "scalp_micro_breakout",
                        "validation_passed": False,
                        "multiplicity_survivor": False,
                        "p_value": 0.5817,
                        "family_id": "fam_gold_scalp_m15",
                    },
                    {
                        "name": "Donchian 20",
                        "strategy": "breakout",
                        "validation_passed": True,
                        "multiplicity_survivor": True,
                        "p_value": 0.0001,
                        "family_id": "fam_test",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _client(tmp_path: Path):
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        storage_backend="memory",
        execution_backend="mock",
        strategies="breakout",
        research_report_path=_report(tmp_path),
    )
    return AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")


def _row(payload: dict, name: str) -> dict:
    return next(row for row in payload["strategies"] if row["name"] == name)


def test_panel_reports_verdicts_and_locks_unvalidated(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            body = (await client.get("/api/v1/strategies/effective")).json()
            assert body["configured"] == ["breakout"]
            assert body["validated_count"] == 1  # only the survivor

            survivor = _row(body, "breakout")
            assert survivor["validation_status"] == "promotable"
            assert survivor["validated"] is True
            assert survivor["locked"] is False

            rejected = _row(body, "scalp_micro_breakout")
            assert rejected["validation_status"] == "rejected"
            assert rejected["locked"] is True
            assert "multiplicity-adjusted" in rejected["lock_reason"]
            assert rejected["last_p_value"] == 0.5817

            unknown = _row(body, "sma_crossover")
            assert unknown["validation_status"] == "unvalidated"
            assert unknown["locked"] is True
            assert "unvalidated" in unknown["lock_reason"]

    asyncio.run(run())


def test_enabling_an_unvalidated_family_is_refused_server_side(tmp_path: Path) -> None:
    """The promotion contract is enforced by the API, not merely hidden in the dashboard."""

    async def run() -> None:
        async with _client(tmp_path) as client:
            refused = await client.post("/api/v1/strategies/scalp_micro_breakout/enable")
            assert refused.status_code == 409
            assert "multiplicity" in refused.json()["detail"]

            never_tested = await client.post("/api/v1/strategies/sma_crossover/enable")
            assert never_tested.status_code == 409

            unknown = await client.post("/api/v1/strategies/not_a_strategy/enable")
            assert unknown.status_code == 409  # also unvalidated; unknown names never enable

            # Disabling is always allowed: the safe direction must never be blocked.
            off = await client.post("/api/v1/strategies/breakout/disable")
            assert off.status_code == 200
            assert off.json()["enabled"] is False

    asyncio.run(run())


def test_survivor_can_be_enabled_and_disabled(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            on = await client.post("/api/v1/strategies/breakout/enable")
            assert on.status_code == 200
            assert on.json()["enabled"] is True
            row = _row((await client.get("/api/v1/strategies/effective")).json(), "breakout")
            assert row["enabled"] is True
            off = await client.post("/api/v1/strategies/breakout/disable")
            assert off.status_code == 200

    asyncio.run(run())


def test_panel_builds_without_a_report(tmp_path: Path) -> None:
    """No report means unvalidated, never 'validated by default'."""

    async def run() -> None:
        settings = Settings(
            trading_mode=TradingMode.DEMO,
            storage_backend="memory",
            execution_backend="mock",
            strategies="breakout",
            research_report_path=str(tmp_path / "missing.json"),
        )
        client = AsyncClient(
            transport=ASGITransport(app=create_app(settings)), base_url="http://test"
        )
        async with client:
            body = (await client.get("/api/v1/strategies/effective")).json()
            assert body["validated_count"] == 0
            assert all(row["validation_status"] == "unvalidated" for row in body["strategies"])
            refused = await client.post("/api/v1/strategies/breakout/enable")
            assert refused.status_code == 409

    asyncio.run(run())
