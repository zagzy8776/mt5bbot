"""Autonomous web research with provenance, budgets and no influence on live configuration.

This layer reads public pages the operator allow-lists, turns them into structured findings that
carry the URL, fetch time and content hash they came from, and stops there. Findings are hypotheses
to test on recorded outcomes — they never edit configuration, never touch the risk engine and are
never read by the trading loop.

Hard limits are structural, not advisory: a mandatory allow-list (empty = no requests at all), a
per-page byte cap, a total byte budget per run, a source cap per run and a request timeout. Any
failure is recorded as an error and returns no content rather than a guess.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from mt5_platform.common.events import ResearchNote

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WHITESPACE = re.compile(r"\s+")

DEFAULT_SUMMARY_CHARS = 600
DEFAULT_EXCERPT_CHARS = 2000


def html_to_text(markup: str) -> str:
    """Strip markup to readable text (no external parser, deterministic, bounded)."""
    if not markup:
        return ""
    text = _SCRIPT_STYLE.sub(" ", markup)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    return _WHITESPACE.sub(" ", text).strip()


def extract_title(markup: str, fallback: str) -> str:
    match = _TITLE.search(markup or "")
    if not match:
        return fallback
    title = html.unescape(_WHITESPACE.sub(" ", _TAGS.sub(" ", match.group(1)))).strip()
    return title or fallback


def content_hash(text: str) -> str:
    """Stable identity of what was read, so the same page cannot be stored twice."""
    return hashlib.sha256(_WHITESPACE.sub(" ", text or "").strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WebSource:
    """One allow-listed page to read, with the tags a reviewer will see."""

    url: str
    kind: str = "intel"  # intel | research | documentation
    tags: tuple[str, ...] = ()
    note: str = ""

    def domain(self) -> str:
        return urlparse(self.url).netloc.lower()


@dataclass
class FetchResult:
    url: str
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: int = 0
    text: str = ""
    raw_bytes: int = 0
    truncated: bool = False
    error: str = ""

    def ok(self) -> bool:
        return self.status == 200 and bool(self.text) and not self.error


class Fetcher(Protocol):
    """Fetches page bodies. Implementations must enforce their own limits."""

    name: str

    def fetch(self, url: str, *, max_bytes: int) -> FetchResult:  # pragma: no cover
        ...


class StaticFetcher:
    """Fetcher over in-memory pages (tests, offline runs). Never touches the network."""

    name = "static"

    def __init__(self, pages: dict[str, str], *, status: int = 200) -> None:
        self.pages = dict(pages)
        self.status = status

    def fetch(self, url: str, *, max_bytes: int) -> FetchResult:
        if url not in self.pages:
            return FetchResult(url=url, status=404, error="not_found")
        body = self.pages[url].encode("utf-8")
        truncated = len(body) > max_bytes
        return FetchResult(
            url=url,
            status=self.status,
            text=body[:max_bytes].decode("utf-8", errors="replace"),
            raw_bytes=len(body),
            truncated=truncated,
        )


class HttpFetcher:
    """Fetches a page over HTTP with a mandatory allow-list, timeout and byte cap."""

    name = "http"

    def __init__(
        self,
        *,
        allow: list[str],
        timeout_s: float = 8.0,
        name: str = "http",
        client: Any | None = None,
    ) -> None:
        self.allow = [prefix.strip().lower() for prefix in allow if prefix.strip()]
        self.timeout_s = float(timeout_s)
        self.name = name
        self._client = client

    def url_allowed(self, url: str) -> bool:
        lowered = (url or "").strip().lower()
        return bool(self.allow) and any(lowered.startswith(prefix) for prefix in self.allow)

    def fetch(self, url: str, *, max_bytes: int) -> FetchResult:
        if not self.url_allowed(url):
            return FetchResult(url=url, error="url_not_allow_listed")
        if self._client is None:
            try:
                import httpx  # lazy: research must never be a trading-path dependency
            except ImportError:  # pragma: no cover - httpx is a project dependency
                return FetchResult(url=url, error="httpx_unavailable")
            self._client = httpx.Client(timeout=self.timeout_s, follow_redirects=False)
        headers = {"User-Agent": "mt5-platform-research/1.0"}
        stream = getattr(self._client, "stream", None)
        if callable(stream):
            return self._stream_bounded(url, max_bytes, headers, stream)
        try:  # fallback for simple clients: read, then slice (the client already buffered it)
            response = self._client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - a fetch failure is data, not a crash
            return FetchResult(url=url, error=f"{type(exc).__name__}:{exc}")
        body = bytes(getattr(response, "content", b"") or b"")
        return FetchResult(
            url=url,
            status=int(getattr(response, "status_code", 0) or 0),
            text=body[:max_bytes].decode("utf-8", errors="replace"),
            raw_bytes=len(body),
            truncated=len(body) > max_bytes,
        )

    def _stream_bounded(
        self, url: str, max_bytes: int, headers: dict[str, str], stream: Any
    ) -> FetchResult:
        """Stream the body and stop at the byte cap, so a huge page never lands in memory."""
        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            with stream("GET", url, headers=headers) as response:
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    remaining = max_bytes - total
                    if remaining <= 0:
                        truncated = True
                        break
                    take = chunk[:remaining]
                    chunks.append(take)
                    total += len(take)
                    if len(take) < len(chunk):
                        truncated = True
                        break
                status = int(getattr(response, "status_code", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(url=url, error=f"{type(exc).__name__}:{exc}")
        body = b"".join(chunks)
        return FetchResult(
            url=url,
            status=status,
            text=body.decode("utf-8", errors="replace"),
            raw_bytes=total,
            truncated=truncated,
        )


@dataclass
class WebIntelReport:
    """What one collection run did, including the budget it respected."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    fetcher: str = ""
    requested: int = 0
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    skipped: int = 0
    bytes_read: int = 0
    budget_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[ResearchNote] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "fetcher": self.fetcher,
            "requested": self.requested,
            "fetched": self.fetched,
            "stored": self.stored,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "bytes_read": self.bytes_read,
            "budget_bytes": self.budget_bytes,
            "errors": list(self.errors),
            "note_ids": [n.note_id for n in self.notes],
            "urls": [n.url for n in self.notes],
        }


class WebIntelCollector:
    """Reads allow-listed sources within explicit budgets and stores findings with provenance."""

    def __init__(
        self,
        *,
        store: Any | None,
        fetcher: Fetcher | None,
        max_sources: int = 10,
        max_bytes_per_page: int = 200_000,
        max_total_bytes: int = 1_000_000,
        summary_chars: int = DEFAULT_SUMMARY_CHARS,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.max_sources = int(max_sources)
        self.max_bytes_per_page = int(max_bytes_per_page)
        self.max_total_bytes = int(max_total_bytes)
        self.summary_chars = int(summary_chars)
        self.excerpt_chars = int(excerpt_chars)
        self.runs = 0
        self.last_report: WebIntelReport | None = None

    def _allowed(self, source: WebSource) -> bool:
        checker = getattr(self.fetcher, "url_allowed", None)
        return True if not callable(checker) else bool(checker(source.url))

    async def collect(self, sources: list[WebSource]) -> WebIntelReport:
        report = WebIntelReport(fetcher=getattr(self.fetcher, "name", "none"))
        report.budget_bytes = self.max_total_bytes
        report.requested = len(sources)
        if self.fetcher is None:
            report.errors.append("no_fetcher_configured")
            self.last_report = report
            return report
        for source in sources[: self.max_sources]:
            if not self._allowed(source):
                report.skipped += 1
                report.errors.append(f"url_not_allow_listed:{source.url[:60]}")
                continue
            if report.bytes_read >= self.max_total_bytes:
                report.skipped += len(sources) - report.fetched - report.skipped
                report.errors.append("total_byte_budget_exhausted")
                break
            remaining = max(
                1, min(self.max_bytes_per_page, self.max_total_bytes - report.bytes_read)
            )
            try:
                result = self.fetcher.fetch(source.url, max_bytes=remaining)
            except Exception as exc:  # noqa: BLE001 - malformed fetcher must not stop the run
                report.errors.append(f"fetch_failed:{type(exc).__name__}:{exc}")
                continue
            report.bytes_read += result.raw_bytes
            if not result.ok():
                report.errors.append(
                    f"fetch_status:{result.status or result.error}:{source.url[:60]}"
                )
                continue
            report.fetched += 1
            note = self._build_note(source, result)
            report.notes.append(note)
            if self.store is None:
                continue
            try:
                before = await self._existing_hashes()
                await self.store.write_research_note(note)
                if note.content_hash in before:
                    report.duplicates += 1
                else:
                    report.stored += 1
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"store_failed:{type(exc).__name__}:{exc}")
        self.runs += 1
        self.last_report = report
        return report

    async def _existing_hashes(self) -> set[str]:
        getter = getattr(self.store, "get_research_notes", None)
        if not callable(getter):
            return set()
        try:
            return {note.content_hash for note in await getter(limit=500)}
        except Exception:  # noqa: BLE001
            return set()

    def _build_note(self, source: WebSource, result: FetchResult) -> ResearchNote:
        text = html_to_text(result.text)
        title = extract_title(result.text, fallback=source.url)
        return ResearchNote(
            url=source.url,
            domain=source.domain(),
            title=title,
            summary=text[: self.summary_chars],
            text_excerpt=text[: self.excerpt_chars],
            tags=list(source.tags),
            source_kind=source.kind,
            content_hash=content_hash(text or result.text),
            fetched_at=result.fetched_at,
            provenance={
                "fetcher": getattr(self.fetcher, "name", ""),
                "status": result.status,
                "raw_bytes": result.raw_bytes,
                "truncated": result.truncated,
                "source_note": source.note,
                "extractor": "html_to_text/1.0",
            },
        )


@dataclass(frozen=True)
class ResearchQuestion:
    """A hypothesis worth testing, derived from research notes — never a configuration change."""

    question_id: str
    statement: str
    source_urls: tuple[str, ...]
    content_hashes: tuple[str, ...]
    distinct_sources: int
    evidence_quality: str = "insufficient"
    proposed_change_type: None = None  # research cannot propose config edits
    tests_required: tuple[str, ...] = (
        "backtest_on_recorded_bars",
        "walk_forward_validation",
        "out_of_sample_check",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "statement": self.statement,
            "source_urls": list(self.source_urls),
            "content_hashes": list(self.content_hashes),
            "distinct_sources": self.distinct_sources,
            "evidence_quality": self.evidence_quality,
            "proposed_change_type": self.proposed_change_type,
            "tests_required": list(self.tests_required),
            "applies_to_config": False,
        }


def propose_research_questions(
    notes: list[ResearchNote], *, max_proposals: int = 5, min_chars: int = 60
) -> list[ResearchQuestion]:
    """Turn notes into testable questions, keeping the source that produced each one.

    A note with too little text is dropped rather than summarised into something it did not say.
    Evidence quality never claims validation: a single source stays "insufficient" no matter how
    confident its wording sounds.
    """
    questions: list[ResearchQuestion] = []
    seen: set[str] = set()
    for note in notes[: max(0, max_proposals)]:
        text = (note.summary or note.text_excerpt or "").strip()
        if len(text) < min_chars:
            continue
        first_sentence = text.split(". ")[0].strip()
        statement = f"Test whether: {first_sentence[:200]}"
        key = content_hash(statement)
        if key in seen:
            continue
        seen.add(key)
        questions.append(
            ResearchQuestion(
                question_id=f"rq_{key[:12]}",
                statement=statement,
                source_urls=(note.url,),
                content_hashes=(note.content_hash,),
                distinct_sources=1,
                evidence_quality="insufficient",
            )
        )
    return questions


def build_fetcher(settings: Any, *, client: Any | None = None) -> Fetcher | None:
    """Fetcher from configuration: nothing is fetched unless enabled, allow-listed and sourced."""
    if not getattr(settings, "web_intel_enabled", False):
        return None
    allow = list(getattr(settings, "web_intel_allow", []) or [])
    sources = list(getattr(settings, "web_intel_sources", []) or [])
    if not allow or not sources:
        return None
    return HttpFetcher(
        allow=allow,
        timeout_s=float(getattr(settings, "web_intel_timeout_s", 8.0)),
        client=client,
    )


def sources_from_settings(settings: Any) -> list[WebSource]:
    """Allow-listed sources from configuration. An empty allow-list yields no sources at all."""
    allow = [
        a.strip().lower() for a in (getattr(settings, "web_intel_allow", []) or []) if a.strip()
    ]
    if not allow:
        return []
    out: list[WebSource] = []
    for url in getattr(settings, "web_intel_sources", []) or []:
        if not url or not url.strip():
            continue
        if not any(url.strip().lower().startswith(prefix) for prefix in allow):
            continue  # unlisted URLs are dropped here, not at fetch time
        out.append(WebSource(url=url.strip()))
    return out


__all__ = [
    "DEFAULT_EXCERPT_CHARS",
    "DEFAULT_SUMMARY_CHARS",
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
