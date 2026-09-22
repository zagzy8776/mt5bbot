"""Phase 4: news/macro ingestion, dedup, provenance and the opt-in blackout protection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mt5_platform.common.events import AccountSnapshot, NewsEvent, OrderSide, StrategySignal
from mt5_platform.config import Settings
from mt5_platform.ingestion.news import (
    FileNewsProvider,
    HttpNewsProvider,
    NewsIngestor,
    StaticNewsProvider,
    blackout_state,
    build_news_provider,
    currencies_for_symbol,
    dedup_key_for,
    normalize_event,
)
from mt5_platform.risk import RiskContext, RiskEngine
from mt5_platform.storage import InMemoryMarketDataStore

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _event(
    *,
    title: str = "US CPI y/y",
    impact: str = "high",
    currency: str = "USD",
    minutes: float = 10.0,
    event_type: str = "inflation",
) -> NewsEvent:
    published = NOW + timedelta(minutes=minutes)
    return NewsEvent(
        published_at=published,
        fetched_at=NOW,
        source="test",
        provider="static",
        currencies=[currency],
        title=title,
        impact=impact,
        event_type=event_type,
        dedup_key=dedup_key_for(
            published_at=published,
            currencies=[currency],
            title=title,
            event_type=event_type,
        ),
    )


# ------------------------------------------------------------------- normalisation


def test_normalize_event_keeps_what_the_provider_said() -> None:
    event = normalize_event(
        {
            "date": "2026-09-22T12:30:00Z",
            "currency": "USD",
            "title": "Fed Interest Rate Decision",
            "impact": "High",
            "actual": "4.25%",
            "forecast": "4.25%",
            "previous": "4.50%",
            "url": "https://example.test/fed",
            "country": "US",
        },
        provider="file",
        source="file:calendar.json",
        now=NOW,
    )
    assert event is not None
    assert event.published_at == datetime(2026, 9, 22, 12, 30, tzinfo=UTC)
    assert event.currencies == ["USD"]
    assert event.impact == "high"  # normalised casing, no invented level
    assert event.actual == "4.25%" and event.previous == "4.50%"
    assert event.provider == "file" and event.source == "file:calendar.json"
    assert event.dedup_key  # stable identity for the row


def test_normalize_event_accepts_epoch_seconds_and_milliseconds() -> None:
    seconds = normalize_event(
        {"time": 1790085000, "title": "NFP", "currency": "USD"}, provider="file", source="s"
    )
    millis = normalize_event(
        {"time": 1790085000000, "title": "NFP", "currency": "USD"}, provider="file", source="s"
    )
    assert seconds is not None and millis is not None
    assert seconds.published_at == millis.published_at


def test_normalize_event_refuses_payloads_without_time_or_title() -> None:
    assert normalize_event({"title": "no time"}, provider="file", source="s") is None
    assert normalize_event({"date": "2026-09-22T12:00:00Z"}, provider="file", source="s") is None
    assert (
        normalize_event({"date": "not-a-date", "title": "x"}, provider="file", source="s") is None
    )
    assert normalize_event(None, provider="file", source="s") is None  # type: ignore[arg-type]


def test_dedup_key_is_stable_and_event_specific() -> None:
    published = NOW
    a = dedup_key_for(published_at=published, currencies=["USD"], title="CPI", event_type="x")
    b = dedup_key_for(published_at=published, currencies=["usd"], title=" cpi ", event_type="X")
    c = dedup_key_for(published_at=published, currencies=["EUR"], title="CPI", event_type="x")
    assert a == b  # same event, different formatting
    assert a != c  # different currency


def test_currencies_for_symbol_handles_broker_suffixes() -> None:
    assert currencies_for_symbol("XAUUSD") == ["USD"]
    assert currencies_for_symbol("XAUUSDm") == ["USD"]
    assert currencies_for_symbol("EURUSD") == ["EUR", "USD"]
    assert currencies_for_symbol("US30") == ["USD"]
    assert currencies_for_symbol("WEIRD") == []
    assert currencies_for_symbol("") == []


# ----------------------------------------------------------------------- providers


def test_file_provider_reads_both_payload_shapes(tmp_path: Any) -> None:
    window = (NOW - timedelta(hours=1), NOW + timedelta(hours=48))
    array_file = tmp_path / "a.json"
    array_file.write_text(
        '[{"date":"2026-09-22T12:30:00Z","title":"CPI","currency":"USD","impact":"high"}]',
        encoding="utf-8",
    )
    wrapped_file = tmp_path / "b.json"
    wrapped_file.write_text(
        '{"events":[{"date":"2026-09-22T12:30:00Z","title":"CPI","currency":"USD"}]}',
        encoding="utf-8",
    )
    assert len(FileNewsProvider(array_file).fetch(since=window[0], until=window[1])) == 1
    assert len(FileNewsProvider(wrapped_file).fetch(since=window[0], until=window[1])) == 1


def test_file_provider_missing_file_and_window_filter(tmp_path: Any) -> None:
    missing = FileNewsProvider(tmp_path / "nope.json")
    assert missing.fetch(since=NOW, until=NOW + timedelta(days=1)) == []
    provider = FileNewsProvider(tmp_path / "c.json")
    (tmp_path / "c.json").write_text(
        '[{"date":"2026-09-22T12:30:00Z","title":"CPI","currency":"USD"}]', encoding="utf-8"
    )
    assert provider.fetch(since=NOW + timedelta(hours=5), until=NOW + timedelta(hours=6)) == []


def test_http_provider_requires_an_allow_list() -> None:
    provider = HttpNewsProvider("https://example.test/calendar.json", allow=[])
    assert provider.url_allowed() is False
    assert provider.fetch(since=NOW, until=NOW + timedelta(hours=1)) == []


def test_http_provider_refuses_urls_outside_the_allow_list() -> None:
    provider = HttpNewsProvider(
        "https://evil.test/calendar.json", allow=["https://example.test/"]
    )
    assert provider.url_allowed() is False
    assert provider.fetch(since=NOW, until=NOW + timedelta(hours=1)) == []


class _StubResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = body
        self.status_code = status


class _StubClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls = 0

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_http_provider_parses_an_allowed_response() -> None:
    body = b'[{"date":"2026-09-22T12:30:00Z","title":"CPI","currency":"USD","impact":"high"}]'
    client = _StubClient(_StubResponse(body))
    provider = HttpNewsProvider(
        "https://example.test/calendar.json", allow=["https://example.test/"], client=client
    )
    events = provider.fetch(since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1))
    assert len(events) == 1 and events[0].provider == "http" and client.calls == 1


def test_http_provider_caps_bytes_and_survives_failures() -> None:
    big = b'[{"date":"2026-09-22T12:30:00Z","title":"' + b"x" * 500 + b'"}]'
    capped = HttpNewsProvider(
        "https://example.test/calendar.json",
        allow=["https://example.test/"],
        max_bytes=20,
        client=_StubClient(_StubResponse(big)),
    )
    assert capped.fetch(since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)) == []
    broken = HttpNewsProvider(
        "https://example.test/calendar.json",
        allow=["https://example.test/"],
        client=_StubClient(RuntimeError("network down")),
    )
    assert broken.fetch(since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)) == []
    not_found = HttpNewsProvider(
        "https://example.test/calendar.json",
        allow=["https://example.test/"],
        client=_StubClient(_StubResponse(b"[]", status=503)),
    )
    assert not_found.fetch(since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1)) == []


def test_build_news_provider_respects_configuration() -> None:
    assert build_news_provider(Settings(news_enabled=False)) is None
    assert build_news_provider(Settings(news_enabled=True, news_provider="off")) is None
    assert build_news_provider(Settings(news_enabled=True, news_provider="file")) is None
    assert (
        build_news_provider(
            Settings(news_enabled=True, news_provider="file", news_file_path="x.json")
        )
        is not None
    )
    # http without an allow-list must not produce a provider (no request, no leak)
    assert (
        build_news_provider(
            Settings(news_enabled=True, news_provider="http", news_http_url="https://a.test/x")
        )
        is None
    )
    assert (
        build_news_provider(
            Settings(
                news_enabled=True,
                news_provider="http",
                news_http_url="https://a.test/x",
                news_http_allow=["https://a.test/"],
            )
        )
        is not None
    )


# ----------------------------------------------------------------------- ingestion


async def test_ingestor_dedups_a_refetch_of_the_same_event() -> None:
    store = InMemoryMarketDataStore()
    provider = StaticNewsProvider([_event(), _event()])  # the same event twice in one payload
    ingestor = NewsIngestor(store=store, provider=provider)

    first = await ingestor.run(now=NOW)
    second = await ingestor.run(now=NOW)

    assert first.accepted == 1 and first.duplicates == 1
    assert second.accepted == 1 and second.duplicates == 1
    assert len(await store.get_news_events()) == 1, "a re-fetch cannot duplicate the calendar"


async def test_ingestor_reports_a_provider_failure_without_raising() -> None:
    class Boom:
        name = "boom"

        def fetch(self, *, since: datetime, until: datetime) -> list[NewsEvent]:
            raise RuntimeError("provider exploded")

    ingestor = NewsIngestor(store=InMemoryMarketDataStore(), provider=Boom())
    result = await ingestor.run(now=NOW)
    assert result.fetched == 0 and result.accepted == 0
    assert result.errors and result.errors[0].startswith("fetch_failed")
    assert ingestor.stats()["runs"] == 1


async def test_ingestor_reports_a_store_failure_without_raising() -> None:
    class BrokenStore(InMemoryMarketDataStore):
        async def write_news_event(self, event: NewsEvent) -> None:
            raise RuntimeError("db down")

    ingestor = NewsIngestor(store=BrokenStore(), provider=StaticNewsProvider([_event()]))
    result = await ingestor.run(now=NOW)
    assert result.accepted == 0
    assert any(error.startswith("store_failed") for error in result.errors)


async def test_ingestor_without_a_provider_is_explicitly_dormant() -> None:
    ingestor = NewsIngestor(store=InMemoryMarketDataStore(), provider=None)
    result = await ingestor.run(now=NOW)
    assert result.errors == ["news_provider_not_configured"]
    assert await ingestor.recent(currencies=["USD"]) == []


async def test_ingestor_recent_filters_by_currency() -> None:
    store = InMemoryMarketDataStore()
    provider = StaticNewsProvider(
        [_event(minutes=30), _event(currency="EUR", title="ECB press conference", minutes=30)]
    )
    ingestor = NewsIngestor(store=store, provider=provider)
    await ingestor.run(now=NOW)
    titles = [event.title for event in await ingestor.recent(currencies=["USD"], now=NOW)]
    assert titles == ["US CPI y/y"]


# ------------------------------------------------------------------------ blackout


def test_blackout_blocks_before_and_after_a_high_impact_event() -> None:
    events = [_event(minutes=10)]
    before = blackout_state(
        events, now=NOW, currencies=["USD"], before_minutes=30, after_minutes=15
    )
    assert before.blocked and before.reason.startswith("news_blackout_before")
    assert before.minutes_to_event == 10.0

    after = blackout_state(
        events,
        now=NOW + timedelta(minutes=20),
        currencies=["USD"],
        before_minutes=30,
        after_minutes=15,
    )
    assert after.blocked and after.reason.startswith("news_blackout_after")

    outside = blackout_state(
        events,
        now=NOW + timedelta(minutes=60),
        currencies=["USD"],
        before_minutes=30,
        after_minutes=15,
    )
    assert outside.blocked is False


def test_blackout_ignores_other_currencies_impacts_and_unknown_instruments() -> None:
    other_currency = [_event(currency="EUR", minutes=5)]
    assert (
        blackout_state(
            other_currency, now=NOW, currencies=["USD"], before_minutes=30, after_minutes=15
        ).blocked
        is False
    )
    medium = [_event(impact="medium", minutes=5)]
    assert (
        blackout_state(
            medium, now=NOW, currencies=["USD"], before_minutes=30, after_minutes=15
        ).blocked
        is False
    )
    assert (
        blackout_state(
            medium,
            now=NOW,
            currencies=["USD"],
            before_minutes=30,
            after_minutes=15,
            min_impact="medium",
        ).blocked
        is True
    )
    # an unknown instrument (no currencies) must never be "protected" by a guess
    assert (
        blackout_state(
            medium,
            now=NOW,
            currencies=[],
            before_minutes=30,
            after_minutes=15,
            min_impact="medium",
        ).blocked
        is False
    )
    assert (
        blackout_state([], now=NOW, currencies=["USD"], before_minutes=30, after_minutes=15).blocked
        is False
    )


# --------------------------------------------------------------- risk + loop wiring


def _signal() -> StrategySignal:
    return StrategySignal(
        symbol="XAUUSDm",
        direction=OrderSide.BUY,
        entry=2500.0,
        stop_loss=2495.0,
        take_profit=2510.0,
        confidence=0.9,
        strategy_name="breakout",
        timestamp=NOW,
    )


def _risk_context(*, blackout: bool) -> RiskContext:
    from mt5_platform.common.instruments import DEFAULT_SPECS

    return RiskContext(
        account=AccountSnapshot(
            timestamp=NOW,
            balance=10_000.0,
            equity=10_000.0,
            free_margin=10_000.0,
            used_margin=0.0,
            floating_pnl=0.0,
        ),
        open_positions=0,
        current_spread=30.0,
        data_age_ms=100.0,
        market_session_ok=True,
        proposed_volume=0.01,
        instrument=DEFAULT_SPECS["XAUUSD"],
        execution_entry=2500.0,
        news_blackout=blackout,
        news_blackout_reason="news_blackout_before:high:US CPI y/y" if blackout else "",
    )


def test_news_blackout_refuses_entries_only_when_the_protection_is_enabled() -> None:
    enabled = RiskEngine(settings=Settings(execution_backend="mock", news_blackout_enabled=True))
    blocked = enabled.evaluate(_signal(), _risk_context(blackout=True))
    assert blocked.approved is False and "news_blackout" in blocked.reasons
    # same engine, no scheduled event → approved
    assert enabled.evaluate(_signal(), _risk_context(blackout=False)).approved is True

    disabled = RiskEngine(settings=Settings(execution_backend="mock", news_blackout_enabled=False))
    assert disabled.evaluate(_signal(), _risk_context(blackout=True)).approved is True


async def test_loop_polls_news_and_exposes_the_state() -> None:
    from tests.test_dynamic_management import _rig

    store = InMemoryMarketDataStore()
    ingestor = NewsIngestor(store=store, provider=StaticNewsProvider([_event(minutes=30)]))
    loop, _fake, _adapter, _recorder, _store = _rig(news_ingestor=ingestor)

    await loop.run_once()

    state = loop.news_state
    assert state["ingest"]["accepted"] == 1
    assert state["ingest"]["provider"] == "static"
    assert state["ingestor"]["runs"] == 1


async def test_loop_blackout_state_follows_the_event_window() -> None:
    from mt5_platform.common.events import utc_now
    from tests.test_dynamic_management import _rig

    # The loek loop compares against the real clock, so build the event relative to it.
    published = utc_now() + timedelta(minutes=10)
    event = NewsEvent(
        published_at=published,
        fetched_at=utc_now(),
        source="test",
        provider="static",
        currencies=["USD"],
        title="US CPI y/y",
        impact="high",
        event_type="inflation",
        dedup_key=dedup_key_for(
            published_at=published,
            currencies=["USD"],
            title="US CPI y/y",
            event_type="inflation",
        ),
    )
    store = InMemoryMarketDataStore()
    ingestor = NewsIngestor(store=store, provider=StaticNewsProvider([event]))
    loop, _fake, _adapter, _recorder, _store = _rig(
        news_ingestor=ingestor, news_blackout_enabled=True
    )
    await loop._poll_news()  # noqa: SLF001

    blocked = await loop._news_blackout_for("XAUUSDm")  # noqa: SLF001
    assert blocked["enabled"] is True
    assert blocked["currencies"] == ["USD"]
    assert blocked["blocked"] is True
    assert blocked["reason"].startswith("news_blackout_before")
    # a different instrument's currencies are unaffected
    assert (await loop._news_blackout_for("EURJPY"))["blocked"] is False  # noqa: SLF001
