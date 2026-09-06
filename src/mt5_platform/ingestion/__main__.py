"""CLI for ingestion workers: python -m mt5_platform.ingestion --workers N."""

from __future__ import annotations

import argparse
import asyncio
import json

from mt5_platform.config import get_settings
from mt5_platform.ingestion.browser_pool import FakeBrowserBackend, SharedBrowserPool
from mt5_platform.ingestion.pipeline import IngestionPipeline
from mt5_platform.ingestion.proxy_manager import ProxyManager
from mt5_platform.ingestion.sources import SyntheticQuoteSource
from mt5_platform.pipeline import MarketDataValidator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "MT5 platform ingestion worker. Emits normalized MarketDataEvent only; "
            "never places trades."
        )
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Bounded concurrency for browser contexts (default: INGESTION_WORKERS)",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=20,
        help="Stop after N synthetic events (dry-run default source)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Symbol for synthetic dry-run (default: DEFAULT_SYMBOL)",
    )
    return parser


async def _amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    workers = args.workers or settings.ingestion_workers
    symbol = (args.symbol or settings.default_symbol).upper()

    source = SyntheticQuoteSource(symbol=symbol, max_events=args.max_events)
    validator = MarketDataValidator(stale_max_age_ms=settings.stale_data_max_age_ms)
    pool = SharedBrowserPool(backend=FakeBrowserBackend())
    proxies = ProxyManager()

    pipeline = IngestionPipeline(
        source,
        validator,
        browser_pool=pool,
        proxy_manager=proxies,
    )
    stats = await pipeline.run_until_complete(workers=workers, timeout_s=60.0)
    print(
        json.dumps(
            {
                "workers": workers,
                "symbol": symbol,
                "received": stats.received,
                "accepted": stats.accepted,
                "rejected": stats.rejected,
                "worker_errors": stats.worker_errors,
                "reject_reasons": stats.reject_reasons,
                "note": "synthetic dry-run; authorize real dashboards before production use",
            },
            indent=2,
        )
    )
    return 0 if stats.worker_errors == 0 else 1


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
