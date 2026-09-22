"""News / macro-calendar ingestion and the news blackout policy.

No source is bundled and nothing is invented: the runtime reads events from a provider the operator
configures — a local JSON file or an HTTP endpoint behind an allow-list with hard limits. Every
event keeps its provenance, and re-fetching can never duplicate a row (stable dedup key).

This MetaTrader5 build exposes no calendar API (checked at runtime), so the broker terminal cannot
be used as the source here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from mt5_platform.common.events import NewsEvent, utc_now
from mt5_platform.storage.base import MarketDataStore

HIGH_IMPACT = {"high", "3", "red"}
MEDIUM_IMPACT = {"medium", "2", "orange"}
_IMPACT_BY_LEVEL = {**{k: "high" for k in HIGH_IMPACT}, **{k: "medium" for k in MEDIUM_IMPACT}}

# Currency exposure of the instruments this bot trades. Unknown symbols yield no currencies, so a
# blackout can never be applied to something we did not understand.
_INSTRUMENT_CURRENCIES: dict[str, list[str]] = {
    "XAUUSD": ["USD"],
    "XAGUSD": ["USD"],
    "US30": ["USD"],
    "NAS100": ["USD"],
    "SPX500": ["USD"],
    "DE40": ["EUR"],
    "UK100": ["GBP"],
    "JP225": ["JPY"],
}
_BROKER_SUFFIXES = ("m", "c", ".raw", "-c", "_i", "pro", "ecn")


def currencies_for_symbol(symbol: str) -> list[str]:
    """Currencies that can move this instrument. Empty when the symbol is unknown."""
    raw = (symbol or "").strip()
    upper = raw.upper()
    if upper in _INSTRUMENT_CURRENCIES:
        return list(_INSTRUMENT_CURRENCIES[upper])
    for suffix in _BROKER_SUFFIXES:
        if upper.endswith(suffix.upper()) and len(upper) > len(suffix):
            stripped = upper[: -len(suffix)]
            if stripped in _INSTRUMENT_CURRENCIES:
                return list(_INSTRUMENT_CURRENCIES[stripped])
    if len(upper) >= 6 and upper[:6].isalpha():
        return [upper[:3], upper[3:6]]  # e.g. EURUSD -> EUR, USD
    return []


def dedup_key_for(
    *, published_at: datetime, currencies: Sequence[str], title: str, event_type: str
) -> str:
    stamp = published_at.astimezone(UTC).isoformat()
    raw = f"{stamp}|{','.join(sorted(c.upper() for c in currencies))}|{title.strip().lower()}"
    raw += f"|{event_type.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _currencies(raw: dict[str, Any]) -> list[str]:
    value = raw.get("currencies") or raw.get("currency")
    if isinstance(value, str):
        parts = [p.strip().upper() for p in value.replace("/", ",").split(",")]
    elif isinstance(value, Sequence):
        parts = [str(p).strip().upper() for p in value]
    else:
        parts = []
    return [p for p in parts if p]


def _impact(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    return _IMPACT_BY_LEVEL.get(text, text)


def normalize_event(
    raw: dict[str, Any], *, provider: str, source: str, now: datetime | None = None
) -> NewsEvent | None:
    """Build a NewsEvent from a provider payload; None when it lacks a time or a title."""
    if not isinstance(raw, dict):
        return None
    published = _parse_time(raw.get("published_at") or raw.get("date") or raw.get("time"))
    title = str(raw.get("title") or raw.get("event") or "").strip()
    if published is None or not title:
        return None
    currencies = _currencies(raw)
    event_type = str(raw.get("event_type") or raw.get("category") or "").strip()
    return NewsEvent(
        published_at=published,
        fetched_at=now or utc_now(),
        source=source,
        provider=provider,
        currencies=currencies,
        country=str(raw.get("country") or "").strip(),
        title=title,
        impact=_impact(raw.get("impact")),
        event_type=event_type,
        actual=raw.get("actual"),
        forecast=raw.get("forecast"),
        previous=raw.get("previous"),
        url=str(raw.get("url") or "") or None,
        dedup_key=dedup_key_for(
            published_at=published, currencies=currencies, title=title, event_type=event_type
        ),
        metadata={"provider_keys": sorted(str(k) for k in raw)},
    )


class NewsProvider(Protocol):
    """A source of calendar events. Implementations must not invent events."""

    name: str

    def fetch(self, *, since: datetime, until: datetime) -> list[NewsEvent]:  # pragma: no cover
        ...


class StaticNewsProvider:
    """Provider over already-normalised events (tests, manual entry)."""

    def __init__(self, events: Sequence[NewsEvent], *, name: str = "static") -> None:
        self.events = list(events)
        self.name = name

    def fetch(self, *, since: datetime, until: datetime) -> list[NewsEvent]:
        return [e for e in self.events if since <= e.published_at <= until]


class FileNewsProvider:
    """Reads a JSON array (or {"events": [...]}) from a local file. No network, no secrets."""

    def __init__(self, path: str | Path, *, name: str = "file") -> None:
        self.path = Path(path)
        self.name = name

    def fetch(self, *, since: datetime, until: datetime) -> list[NewsEvent]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        rows = payload.get("events", []) if isinstance(payload, dict) else payload
        source = f"{self.name}:{self.path.name}"
        events: list[NewsEvent] = []
        for raw in rows or []:
            event = normalize_event(raw, provider="file", source=source)
            if event is not None and since <= event.published_at <= until:
                events.append(event)
        return events


class HttpNewsProvider:
    """Fetches a JSON payload over HTTP with an allow-list, a timeout and a byte cap.

    The allow-list is mandatory: an empty list refuses every request. The byte cap stops a huge
    response from being read into memory, and any failure returns no events instead of a guess.
    """

    def __init__(
        self,
        url: str,
        *,
        allow: Sequence[str],
        timeout_s: float = 5.0,
        max_bytes: int = 200_000,
        name: str = "http",
        client: Any | None = None,
    ) -> None:
        self.url = url
        self.allow = [a.strip().lower() for a in allow if a.strip()]
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.name = name
        self._client = client

    def url_allowed(self) -> bool:
        lowered = self.url.strip().lower()
        return any(lowered.startswith(prefix) for prefix in self.allow)

    def fetch(self, *, since: datetime, until: datetime) -> list[NewsEvent]:
        if not self.allow or not self.url_allowed():
            return []
        if self._client is None:
            try:
                import httpx  # imported lazily: the trading path must not depend on the network
            except ImportError:  # pragma: no cover - httpx is a project dependency
                return []
            self._client = httpx.Client(timeout=self.timeout_s, follow_redirects=False)
        try:
            response = self._client.get(self.url, headers={"User-Agent": "mt5-platform/1.0"})
            if getattr(response, "status_code", 0) != 200:
                return []
            body = response.content[: self.max_bytes]
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return []
        rows = payload.get("events", []) if isinstance(payload, dict) else payload
        events: list[NewsEvent] = []
        for raw in rows or []:
            event = normalize_event(raw, provider="http", source=f"http:{self.url[:80]}")
            if event is not None and since <= event.published_at <= until:
                events.append(event)
        return events


@dataclass
class NewsIngestResult:
    """What one ingestion run did. Failures are reported here, never raised into the loop."""

    provider: str
    at: datetime = field(default_factory=utc_now)
    fetched: int = 0
    accepted: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "at": self.at.isoformat(),
            "fetched": self.fetched,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "errors": list(self.errors),
        }


class NewsIngestor:
    """fetch -> normalise -> dedup -> store. Never raises: trading must not depend on the feed."""

    def __init__(
        self,
        *,
        store: MarketDataStore | None,
        provider: NewsProvider | None,
        look_back_hours: float = 24.0,
        look_ahead_hours: float = 48.0,
        max_events: int = 500,
    ) -> None:
        self.store = store
        self.provider = provider
        self.look_back_hours = float(look_back_hours)
        self.look_ahead_hours = float(look_ahead_hours)
        self.max_events = int(max_events)
        self.last_result: NewsIngestResult | None = None
        self.runs = 0

    async def run(self, *, now: datetime | None = None) -> NewsIngestResult:
        stamp = now or utc_now()
        result = NewsIngestResult(provider=getattr(self.provider, "name", "none"))
        if self.provider is None or self.store is None:
            result.errors.append("news_provider_not_configured")
            self.last_result = result
            return result
        window_start = stamp - timedelta(hours=self.look_back_hours)
        window_end = stamp + timedelta(hours=self.look_ahead_hours)
        try:
            events = list(self.provider.fetch(since=window_start, until=window_end))
        except Exception as exc:  # a provider must never break the runtime
            result.errors.append(f"fetch_failed:{type(exc).__name__}:{exc}")
            self.runs += 1
            self.last_result = result
            return result
        result.fetched = len(events)
        seen: set[str] = set()
        for event in events[: self.max_events]:
            if event.dedup_key in seen:
                result.duplicates += 1
                continue
            seen.add(event.dedup_key)
            try:
                await self.store.write_news_event(event)
                result.accepted += 1
            except Exception as exc:
                result.errors.append(f"store_failed:{type(exc).__name__}:{exc}")
        self.runs += 1
        self.last_result = result
        return result

    async def recent(
        self,
        *,
        currencies: Sequence[str] | None = None,
        hours: float = 24.0,
        now: datetime | None = None,
        limit: int = 200,
    ) -> list[NewsEvent]:
        if self.store is None:
            return []
        getter = getattr(self.store, "get_news_events", None)
        if not callable(getter):
            return []
        try:
            return await getter(
                currencies=list(currencies) if currencies else None,
                since=(now or utc_now()) - timedelta(hours=float(hours)),
                limit=limit,
            )
        except Exception:
            return []

    def stats(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "provider": getattr(self.provider, "name", None),
            "look_back_hours": self.look_back_hours,
            "look_ahead_hours": self.look_ahead_hours,
            "last": self.last_result.to_dict() if self.last_result else None,
        }


@dataclass(frozen=True)
class NewsBlackout:
    """Whether new risk is allowed right now because of scheduled news."""

    blocked: bool = False
    reason: str = ""
    event: NewsEvent | None = None
    minutes_to_event: float | None = None
    window_minutes_before: float = 0.0
    window_minutes_after: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "reason": self.reason,
            "event_title": self.event.title if self.event else None,
            "event_at": self.event.published_at.isoformat() if self.event else None,
            "event_impact": self.event.impact if self.event else None,
            "minutes_to_event": self.minutes_to_event,
            "window_minutes_before": self.window_minutes_before,
            "window_minutes_after": self.window_minutes_after,
        }


def blackout_state(
    events: Sequence[NewsEvent],
    *,
    now: datetime,
    currencies: Sequence[str],
    before_minutes: float,
    after_minutes: float,
    min_impact: str = "high",
) -> NewsBlackout:
    """High-impact events inside [event - before, event + after] block new risk.

    Only events that actually name one of the instrument's currencies count, and only impacts at or
    above ``min_impact`` (high by default). Nothing is blocked when the caller supplies no
    currencies or no events — an unknown instrument is never "protected" by a guess.
    """
    window = {
        "window_minutes_before": before_minutes,
        "window_minutes_after": after_minutes,
    }
    if not events or not currencies:
        return NewsBlackout(**window)
    allowed = {"high"} if min_impact == "high" else {"high", "medium"}
    relevant = [e for e in events if e.impact in allowed and e.affects(currencies)]
    if not relevant:
        return NewsBlackout(**window)
    for event in sorted(relevant, key=lambda e: e.published_at):
        minutes = (event.published_at - now).total_seconds() / 60.0
        if -after_minutes <= minutes <= before_minutes:
            state = "before" if minutes >= 0 else "after"
            return NewsBlackout(
                blocked=True,
                reason=f"news_blackout_{state}:{event.impact}:{event.title[:60]}",
                event=event,
                minutes_to_event=round(minutes, 2),
                window_minutes_before=before_minutes,
                window_minutes_after=after_minutes,
            )
    return NewsBlackout(**window)


def build_news_provider(settings: Any) -> NewsProvider | None:
    """Provider from configuration. ``off``/``disabled`` keeps ingestion dormant."""
    if not getattr(settings, "news_enabled", False):
        return None
    provider = str(getattr(settings, "news_provider", "off")).strip().lower()
    if provider in {"off", "disabled", "none", ""}:
        return None
    if provider == "file":
        path = str(getattr(settings, "news_file_path", "") or "")
        return FileNewsProvider(path) if path else None
    if provider == "http":
        url = str(getattr(settings, "news_http_url", "") or "")
        allow = list(getattr(settings, "news_http_allow", []) or [])
        if not url or not allow:
            return None  # no allow-list, no request
        return HttpNewsProvider(url, allow=allow)
    return None


__all__ = [
    "FileNewsProvider",
    "HttpNewsProvider",
    "NewsBlackout",
    "NewsIngestResult",
    "NewsIngestor",
    "NewsProvider",
    "StaticNewsProvider",
    "blackout_state",
    "build_news_provider",
    "currencies_for_symbol",
    "dedup_key_for",
    "normalize_event",
]
