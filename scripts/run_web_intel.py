"""Autonomous web research run (offline by default; writes notes, proposes nothing to the runtime).

    python scripts/run_web_intel.py --url https://example.org/a --url https://example.org/b
    python scripts/run_web_intel.py --from-settings            # uses WEB_INTEL_* configuration

Every URL must be allow-listed (WEB_INTEL_ALLOW or --allow); with an empty allow-list nothing is
fetched at all. Findings are stored with their URL, fetch time and content hash, and the run prints
the hypotheses it derived. Nothing here can change configuration, risk limits or live trading.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_platform.config import Settings  # noqa: E402
from mt5_platform.intelligence.web import (  # noqa: E402
    HttpFetcher,
    WebIntelCollector,
    WebSource,
    propose_research_questions,
)
from mt5_platform.storage import create_store_from_settings  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    urls = list(args.url or [])
    allow = list(args.allow or [])
    if args.from_settings:
        urls = list(args.url or settings.web_intel_sources)
        allow = list(args.allow or settings.web_intel_allow)
    if not allow:
        print("refused: no allow-list given (--allow or WEB_INTEL_ALLOW); nothing was fetched")
        return 1
    if not urls:
        print("refused: no sources given (--url or WEB_INTEL_SOURCES); nothing was fetched")
        return 1
    allow = [prefix for prefix in allow if any(u.lower().startswith(prefix.lower()) for u in urls)]
    fetcher = HttpFetcher(allow=allow, timeout_s=args.timeout)
    store = create_store_from_settings(settings)
    collector = WebIntelCollector(
        store=store,
        fetcher=fetcher,
        max_sources=args.max_sources,
        max_bytes_per_page=args.max_bytes_per_page,
    )
    report = await collector.collect([WebSource(url=url) for url in urls])
    print(json.dumps(report.to_dict(), indent=2))
    questions = propose_research_questions(report.notes, max_proposals=args.max_proposals)
    for question in questions:
        print(json.dumps(question.to_dict(), indent=2))
    print(
        f"\n{report.stored} new note(s), {report.duplicates} already known, "
        f"{len(questions)} question(s)"
    )
    print("research output is not applied to configuration: promote only after validation")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="autonomous web research with provenance")
    parser.add_argument("--url", action="append", default=[], help="source URL (repeatable)")
    parser.add_argument(
        "--allow", action="append", default=[], help="allow-list prefix (repeatable)"
    )
    parser.add_argument("--from-settings", action="store_true", help="use WEB_INTEL_* settings")
    parser.add_argument("--max-sources", type=int, default=10)
    parser.add_argument("--max-bytes-per-page", type=int, default=200_000)
    parser.add_argument("--max-proposals", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
