"""Data ingestion subsystem (M1–M3). Produces MarketDataEvent only — never trades."""

from __future__ import annotations

from mt5_platform.ingestion.browser_pool import (
    FakeBrowserBackend,
    PlaywrightBrowserBackend,
    PooledContext,
    SharedBrowserPool,
)
from mt5_platform.ingestion.context_config import ContextConfig
from mt5_platform.ingestion.pipeline import IngestionPipeline, IngestionStats
from mt5_platform.ingestion.proxy_manager import ProxyManager, ProxyRecord
from mt5_platform.ingestion.resource_policy import (
    ResourceClass,
    attach_resource_filter,
    classify_resource,
    should_abort,
)
from mt5_platform.ingestion.retry import retry_async
from mt5_platform.ingestion.sources import (
    CallableQuoteSource,
    MarketDataSource,
    SyntheticQuoteSource,
)

__all__ = [
    "CallableQuoteSource",
    "ContextConfig",
    "FakeBrowserBackend",
    "IngestionPipeline",
    "IngestionStats",
    "MarketDataSource",
    "PlaywrightBrowserBackend",
    "PooledContext",
    "ProxyManager",
    "ProxyRecord",
    "ResourceClass",
    "SharedBrowserPool",
    "SyntheticQuoteSource",
    "attach_resource_filter",
    "classify_resource",
    "retry_async",
    "should_abort",
]
