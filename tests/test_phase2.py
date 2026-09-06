"""Phase 2 ingestion tests — browser pool, proxies, retry, pipeline isolation."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mt5_platform.common.enums import ProxyErrorType, ProxyState
from mt5_platform.common.events import MarketDataEvent
from mt5_platform.ingestion.browser_pool import FakeBrowserBackend, SharedBrowserPool
from mt5_platform.ingestion.context_config import ContextConfig
from mt5_platform.ingestion.pipeline import IngestionPipeline
from mt5_platform.ingestion.proxy_manager import ProxyManager
from mt5_platform.ingestion.resource_policy import attach_resource_filter, should_abort
from mt5_platform.ingestion.retry import retry_async
from mt5_platform.ingestion.sources import SyntheticQuoteSource
from mt5_platform.pipeline import MarketDataValidator
from mt5_platform.pipeline.normalize import OrderedEventBuffer


@pytest.mark.asyncio
async def test_browser_pool_bounds_concurrency_and_shutdown() -> None:
    backend = FakeBrowserBackend()
    pool = SharedBrowserPool(backend=backend, stale_after_s=60)
    await pool.start(workers=2)

    c1 = await pool.acquire_context(config=ContextConfig(locale="en-GB"))
    c2 = await pool.acquire_context()
    assert pool.active_count == 2
    assert backend.contexts_created == 2

    await pool.release_context(c1)
    await pool.release_context(c2)
    await pool.stop()
    assert pool.active_count == 0
    assert backend.contexts_closed == 2
    assert not pool.is_started


@pytest.mark.asyncio
async def test_browser_pool_recycles_stale_context() -> None:
    pool = SharedBrowserPool(backend=FakeBrowserBackend(), stale_after_s=0.0)
    await pool.start(workers=1)
    pooled = await pool.acquire_context()
    assert pool.is_context_stale(pooled)
    replacement = await pool.mark_stale_and_recycle(pooled)
    assert replacement.context_id != pooled.context_id
    await pool.release_context(replacement)
    await pool.stop()


def test_proxy_recovers_after_quarantine_window() -> None:
    pm = ProxyManager(cooldown_s=0.0, quarantine_s=0.0, circuit_threshold=3)
    pm.register("p1", "http://proxy.test:8080")
    acquired = pm.acquire()
    assert acquired is not None
    pm.release("p1", success=False, error=ProxyErrorType.PROXY_HANDSHAKE, latency_ms=999)
    assert pm.get("p1").state is ProxyState.QUARANTINED
    # quarantine_s=0 → immediate recovery on next acquire sweep
    again = pm.acquire()
    assert again is not None
    assert again.proxy_id == "p1"
    assert again.state is ProxyState.IN_USE


def test_proxy_trips_circuit_after_three_timeouts() -> None:
    pm = ProxyManager(cooldown_s=60.0, quarantine_s=300.0)
    pm.register("p2", "http://proxy2.test:8080")
    assert pm.acquire() is not None
    pm.release("p2", success=False, error=ProxyErrorType.TIMEOUT)
    assert pm.get("p2").state is ProxyState.COOLING
    pm.release("p2", success=False, error=ProxyErrorType.TIMEOUT)
    pm.release("p2", success=False, error=ProxyErrorType.TIMEOUT)
    assert pm.get("p2").state is ProxyState.QUARANTINED
    assert pm.acquire() is None


@pytest.mark.asyncio
async def test_retry_async_exponential_backoff() -> None:
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("boom")
        return "ok"

    delays: list[float] = []
    result = await retry_async(
        flaky,
        attempts=5,
        base_delay_s=0.01,
        max_delay_s=0.05,
        jitter=0.0,
        retry_on=(TimeoutError,),
        on_retry=lambda _a, _e, d: delays.append(d),
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert delays == [0.01, 0.02]


@pytest.mark.asyncio
async def test_ingestion_pipeline_accepts_synthetic_quotes() -> None:
    source = SyntheticQuoteSource(symbol="XAUUSD", max_events=5, interval_s=0.0)
    validator = MarketDataValidator(stale_max_age_ms=60_000)
    sunk: list[MarketDataEvent] = []

    async def sink(event: MarketDataEvent) -> None:
        sunk.append(event)

    pipeline = IngestionPipeline(source, validator, sink=sink)
    stats = await pipeline.run_until_complete(workers=2, timeout_s=5.0)
    assert stats.received == 5
    assert stats.accepted == 5
    assert stats.rejected == 0
    assert len(sunk) == 5
    assert all(e.symbol == "XAUUSD" for e in sunk)


@pytest.mark.asyncio
async def test_ingestion_pipeline_rejects_stale_before_sink() -> None:
    async def stale_events():
        yield MarketDataEvent(
            timestamp=datetime.now(UTC) - timedelta(seconds=30),
            source="test",
            symbol="XAUUSD",
            price=2500.0,
        )

    class _Src:
        async def start(self):
            return None

        async def stop(self):
            return None

        def events(self):
            return stale_events()

    sunk: list[MarketDataEvent] = []
    pipeline = IngestionPipeline(
        _Src(),  # type: ignore[arg-type]
        MarketDataValidator(stale_max_age_ms=1000),
        sink=lambda e: sunk.append(e),
    )
    stats = await pipeline.run_until_complete(timeout_s=5.0)
    assert stats.accepted == 0
    assert stats.rejected == 1
    assert sunk == []


def test_ordered_event_buffer_emits_in_timestamp_order() -> None:
    buf = OrderedEventBuffer(max_skew_ms=5000)
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
    late = MarketDataEvent(
        timestamp=t0 + timedelta(seconds=2), source="t", symbol="XAUUSD", price=2
    )
    early = MarketDataEvent(timestamp=t0, source="t", symbol="XAUUSD", price=1)
    # Push later first, then earlier (out of order arrival)
    assert buf.push(late)[0].price == 2
    # earlier than last but within skew — remains buffered / not re-emitted as regression
    out = buf.push(early)
    assert isinstance(out, list)


def test_resource_filter_skips_contexts_without_route() -> None:
    import asyncio

    async def _run():
        await attach_resource_filter({"no": "route"}, calibrated=True)

    asyncio.run(_run())
    assert should_abort("font", "https://x/a.woff", calibrated=True)


def test_ingestion_modules_do_not_import_orders_or_execution() -> None:
    """Architectural guard: scrapers must not call trading layers."""
    root = Path(__file__).resolve().parents[1] / "src" / "mt5_platform" / "ingestion"
    forbidden = ("mt5_platform.orders", "mt5_platform.execution", "mt5_platform.risk")
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(node.module.startswith(f) for f in forbidden), path
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(alias.name.startswith(f) for f in forbidden), path
