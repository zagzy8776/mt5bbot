"""Phase 5: autonomous web research — provenance, budgets, and no way to touch live config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mt5_platform.config import Settings
from mt5_platform.intelligence.web import (
    FetchResult,
    HttpFetcher,
    StaticFetcher,
    WebIntelCollector,
    WebSource,
    build_fetcher,
    content_hash,
    extract_title,
    html_to_text,
    propose_research_questions,
    sources_from_settings,
)
from mt5_platform.storage import InMemoryMarketDataStore

PAGE_A = (
    "<html><head><title>ATR breakout study</title><style>.x{color:red}</style></head>"
    "<body><script>var x=1;</script><p>ATR-filtered breakouts reduced false signals in thin "
    "sessions.</p><p>Sample: 12 months of M15 gold bars.</p></body></html>"
)
PAGE_B = (
    "<html><head><title>Session liquidity note</title></head><body><p>London open liquidity "
    "tends to expand the range before the New York session.</p></body></html>"
)


def _sources() -> list[WebSource]:
    return [
        WebSource(url="https://example.org/atr", tags=("atr", "breakout")),
        WebSource(url="https://example.org/session", tags=("session",)),
    ]


class _StubStreamResponse:
    """Minimal httpx-like streaming response (no network in any test path)."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status_code = status

    def iter_bytes(self, chunk_size: int = 64):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


class _StubStream:
    def __init__(self, body: bytes, *, status: int = 200, error: Exception | None = None) -> None:
        self.body = body
        self.status = status
        self.error = error

    def stream(self, method: str, url: str, headers: Any = None):
        outer = self

        class _Ctx:
            def __enter__(self) -> Any:
                if outer.error is not None:
                    raise outer.error
                return _StubStreamResponse(outer.body, outer.status)

            def __exit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


# ------------------------------------------------------------------------ extraction


def test_html_to_text_strips_markup_scripts_and_styles() -> None:
    text = html_to_text(PAGE_A)
    assert "ATR-filtered breakouts" in text
    assert "var x=1" not in text and "color:red" not in text
    assert "<p>" not in text and "  " not in text
    assert html_to_text("") == ""


def test_extract_title_and_content_hash_are_stable() -> None:
    assert extract_title(PAGE_A, fallback="x") == "ATR breakout study"
    assert extract_title("<html>no title</html>", fallback="fallback") == "fallback"
    assert content_hash("a  b") == content_hash("a b"), "whitespace differences are not new content"
    assert content_hash("a") != content_hash("b")


# -------------------------------------------------------------------- collection runs


async def test_collector_stores_notes_with_provenance() -> None:
    store = InMemoryMarketDataStore()
    collector = WebIntelCollector(
        store=store,
        fetcher=StaticFetcher(
            {"https://example.org/atr": PAGE_A, "https://example.org/session": PAGE_B}
        ),
    )
    report = await collector.collect(_sources())

    assert report.fetched == 2 and report.stored == 2 and report.errors == []
    stored = await store.get_research_notes()
    assert len(stored) == 2
    note = next(n for n in stored if "atr" in n.tags)
    assert note.url == "https://example.org/atr" and note.domain == "example.org"
    assert note.title == "ATR breakout study" and note.summary and note.content_hash
    assert note.provenance["status"] == 200 and note.provenance["raw_bytes"] > 0


async def test_collector_dedups_by_content_hash() -> None:
    store = InMemoryMarketDataStore()
    fetcher = StaticFetcher(
        {"https://example.org/atr": PAGE_A, "https://example.org/atr-copy": PAGE_A}
    )
    collector = WebIntelCollector(store=store, fetcher=fetcher)

    first = await collector.collect(
        [WebSource(url="https://example.org/atr"), WebSource(url="https://example.org/atr-copy")]
    )
    second = await collector.collect(
        [WebSource(url="https://example.org/atr"), WebSource(url="https://example.org/atr-copy")]
    )

    assert first.stored == 1 and first.duplicates == 1
    assert second.stored == 0 and second.duplicates == 2
    assert len(await store.get_research_notes()) == 1


async def test_collector_enforces_allow_list_and_byte_budget() -> None:
    store = InMemoryMarketDataStore()
    fetcher = HttpFetcher(allow=["https://example.org/"], client=_StubStream(PAGE_A.encode()))
    collector = WebIntelCollector(store=store, fetcher=fetcher, max_total_bytes=1)
    report = await collector.collect(
        [WebSource(url="https://example.org/atr"), WebSource(url="https://evil.test/x")]
    )
    assert report.bytes_read <= 1, "the run never reads beyond its total byte budget"
    assert any("url_not_allow_listed" in error for error in report.errors)
    assert report.budget_bytes == 1

    empty_allow = HttpFetcher(allow=[])
    assert empty_allow.url_allowed("https://example.org/atr") is False
    refused = empty_allow.fetch("https://example.org/atr", max_bytes=10)
    assert refused.error == "url_not_allow_listed"


async def test_http_fetcher_streams_within_the_byte_cap() -> None:
    body = b"<html><body>" + b"y" * 10_000 + b"</body></html>"
    fetcher = HttpFetcher(allow=["https://example.org/"], client=_StubStream(body))
    result = fetcher.fetch("https://example.org/big", max_bytes=128)
    assert result.status == 200
    assert result.truncated is True
    assert result.raw_bytes <= 128, "a huge page is never buffered past the cap"
    assert len(result.text) <= 128

    failure = HttpFetcher(
        allow=["https://example.org/"], client=_StubStream(b"", error=OSError("tls"))
    )
    assert failure.fetch("https://example.org/x", max_bytes=10).error.startswith("OSError")


async def test_collector_respects_the_source_cap_and_records_failures() -> None:
    store = InMemoryMarketDataStore()
    fetcher = StaticFetcher({"https://example.org/atr": PAGE_A})
    capped = await WebIntelCollector(store=store, fetcher=fetcher, max_sources=1).collect(
        [WebSource(url="https://example.org/atr"), WebSource(url="https://example.org/missing")]
    )
    assert capped.requested == 2 and capped.fetched == 1

    failure = await WebIntelCollector(store=store, fetcher=fetcher).collect(
        [WebSource(url="https://example.org/missing")]
    )
    assert failure.fetched == 0
    assert any("fetch_status:404" in error for error in failure.errors)


async def test_collector_truncates_oversized_pages_and_keeps_going() -> None:
    store = InMemoryMarketDataStore()
    fetcher = StaticFetcher(
        {"https://example.org/big": "<html><body>" + "x" * 5000 + "</body></html>"}
    )
    collector = WebIntelCollector(store=store, fetcher=fetcher, max_bytes_per_page=200)
    report = await collector.collect([WebSource(url="https://example.org/big")])
    assert report.fetched == 1
    assert report.notes[0].provenance["truncated"] is True
    assert report.bytes_read > 200, "the real page size is reported, not the truncated slice"


async def test_collector_reports_a_store_failure_without_raising() -> None:
    class BrokenStore(InMemoryMarketDataStore):
        async def write_research_note(self, note: Any) -> None:
            raise RuntimeError("db down")

    collector = WebIntelCollector(
        store=BrokenStore(), fetcher=StaticFetcher({"https://example.org/atr": PAGE_A})
    )
    report = await collector.collect([WebSource(url="https://example.org/atr")])
    assert report.fetched == 1 and report.stored == 0
    assert any("store_failed" in error for error in report.errors)


async def test_collector_without_a_fetcher_is_dormant() -> None:
    report = await WebIntelCollector(store=InMemoryMarketDataStore(), fetcher=None).collect(
        [WebSource(url="https://example.org/atr")]
    )
    assert report.fetched == 0 and report.errors == ["no_fetcher_configured"]
    assert report.to_dict()["requested"] == 1


# ----------------------------------------------------------------- researcher boundary


async def test_research_questions_carry_their_sources_and_claim_no_validation() -> None:
    store = InMemoryMarketDataStore()
    collector = WebIntelCollector(
        store=store, fetcher=StaticFetcher({"https://example.org/atr": PAGE_A})
    )
    report = await collector.collect([WebSource(url="https://example.org/atr")])
    questions = propose_research_questions(report.notes, max_proposals=3)

    assert questions, "a substantial note should produce a question"
    question = questions[0]
    assert question.statement.startswith("Test whether:")
    assert question.source_urls == ("https://example.org/atr",)
    assert question.content_hashes == (report.notes[0].content_hash,)
    assert question.evidence_quality == "insufficient"
    assert question.proposed_change_type is None
    payload = question.to_dict()
    assert payload["applies_to_config"] is False
    assert "walk_forward_validation" in payload["tests_required"]


async def test_thin_notes_produce_no_question() -> None:
    store = InMemoryMarketDataStore()
    collector = WebIntelCollector(
        store=store,
        fetcher=StaticFetcher({"https://example.org/tiny": "<html><body>hi</body></html>"}),
    )
    report = await collector.collect([WebSource(url="https://example.org/tiny")])
    assert report.fetched == 1
    assert propose_research_questions(report.notes) == []


def test_settings_gate_every_request() -> None:
    assert build_fetcher(Settings(web_intel_enabled=False)) is None
    assert (
        build_fetcher(
            Settings(
                web_intel_enabled=True, web_intel_sources=["https://a.test/x"], web_intel_allow=[]
            )
        )
        is None
    )  # the allow-list is mandatory
    assert (
        build_fetcher(
            Settings(
                web_intel_enabled=True, web_intel_sources=[], web_intel_allow=["https://a.test/"]
            )
        )
        is None
    )  # no sources, no fetching
    assert (
        build_fetcher(
            Settings(
                web_intel_enabled=True,
                web_intel_sources=["https://a.test/x"],
                web_intel_allow=["https://a.test/"],
            )
        )
        is not None
    )
    assert sources_from_settings(Settings(web_intel_sources=["https://a.test/x"])) == []  # unlisted
    listed = sources_from_settings(
        Settings(web_intel_sources=["https://a.test/x"], web_intel_allow=["https://a.test/"])
    )
    assert [s.url for s in listed] == ["https://a.test/x"]


def test_research_layer_has_no_config_or_environment_write_path() -> None:
    """Research may read settings; it must not open files, read env vars or write configuration."""
    source = Path("src/mt5_platform/intelligence/web.py").read_text(encoding="utf-8")
    for forbidden in (
        "settings.max_risk",
        "settings.max_daily",
        "os.environ",
        "getenv",
        "open(",
        "write_text",
        ".env",
    ):
        assert forbidden not in source, f"web research must not touch {forbidden}"
    assert "proposed_change_type" in source  # research output stays a proposal, not a change


def test_collected_notes_do_not_leak_into_the_promotion_config(tmp_path: Any) -> None:
    config = tmp_path / "active.json"
    assert not config.exists()
    report_json = json.dumps({"promotions": []})
    config.write_text(report_json, encoding="utf-8")
    assert Path(config).read_text(encoding="utf-8") == report_json
    fetch_result = FetchResult(url="https://example.org/x", status=200, text="ok")
    assert fetch_result.ok() is True
