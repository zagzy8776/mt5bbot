"""Autonomous research: reading public sources into testable hypotheses (never into config)."""

from mt5_platform.intelligence.web import (
    Fetcher,
    FetchResult,
    HttpFetcher,
    ResearchQuestion,
    StaticFetcher,
    WebIntelCollector,
    WebIntelReport,
    WebSource,
    build_fetcher,
    content_hash,
    extract_title,
    html_to_text,
    propose_research_questions,
    sources_from_settings,
)

__all__ = [
    "FetchResult",
    "Fetcher",
    "HttpFetcher",
    "ResearchQuestion",
    "StaticFetcher",
    "WebIntelCollector",
    "WebIntelReport",
    "WebSource",
    "build_fetcher",
    "content_hash",
    "extract_title",
    "html_to_text",
    "propose_research_questions",
    "sources_from_settings",
]
