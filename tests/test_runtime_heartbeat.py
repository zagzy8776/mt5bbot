"""Runtime heartbeat: fresh vs stale, waiting vs broken, and observational-only by construction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from mt5_platform.api import create_app
from mt5_platform.config import Settings, TradingMode
from mt5_platform.runtime.heartbeat import (
    bar_clock,
    build_runtime_heartbeat,
    parse_timestamp,
    timeframe_seconds,
)

# A Tuesday afternoon (the runtime's clock is UTC-aware throughout).
NOW = datetime(2026, 9, 22, 16, 7, 30, tzinfo=UTC)


def snapshot(
    *,
    state: str = "running",
    timeframe: str = "M15",
    last_processed_bar: str | None = "2026-09-22T15:45:00+00:00",
    stage: str = "candle_evaluated_no_setup",
    generated: int = 0,
    rejected: int = 0,
    orders_sent: int = 0,
    blocked: bool | None = False,
    trade_allowed: bool | None = True,
) -> dict:
    return {
        "state": state,
        "symbol": "XAUUSDM",
        "timeframe": timeframe,
        "connected": True,
        "started_at": "2026-09-22T15:55:52+00:00",
        "stats": {
            "cycles": 162,
            "bars_processed": 3,
            "signals": 0,
            "orders_sent": orders_sent,
            "errors": 0,
            "consecutive_errors": 0,
            "pipeline_stage": stage,
            "pipeline": {
                "cycle": 45,
                "stage": stage,
                "last_processed_bar": last_processed_bar,
                "waiting": True,
                "poll_s": 5.0,
            },
            "signal_engine": {
                "evaluations": 3,
                "signals_generated": generated,
                "signals_rejected": rejected,
                "events_processed": 3,
                "strategy_errors": 0,
                "last_evaluation_time": last_processed_bar,
                "last_rejection": None,
            },
            "execution": {
                "blocked": blocked,
                "terminal": {"trade_allowed": trade_allowed, "reason": ""},
            },
        },
    }


def risk(stats: dict) -> dict:
    return {"stats": stats}


# ------------------------------------------------------------------- timeframe parsing


def test_timeframe_seconds_understands_the_labels_the_runtime_uses() -> None:
    assert timeframe_seconds("M15") == 900
    assert timeframe_seconds("m5") == 300
    assert timeframe_seconds("H1") == 3600
    assert timeframe_seconds("4H") == 14400
    assert timeframe_seconds("") is None
    assert timeframe_seconds("M0") is None
    assert timeframe_seconds("weekly") is None


def test_parse_timestamp_handles_iso_naive_and_junk() -> None:
    assert parse_timestamp("2026-09-22T15:45:00+00:00") == datetime(2026, 9, 22, 15, 45, tzinfo=UTC)
    assert parse_timestamp("2026-09-22T15:45:00Z") == datetime(2026, 9, 22, 15, 45, tzinfo=UTC)
    assert parse_timestamp("2026-09-22T15:45:00") == datetime(2026, 9, 22, 15, 45, tzinfo=UTC)
    assert parse_timestamp("not-a-time") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


def test_bar_clock_names_the_bar_that_should_have_closed() -> None:
    clock = bar_clock(NOW, "M15")
    assert clock is not None
    assert clock.latest_closed_bar_open == datetime(2026, 9, 22, 15, 45, tzinfo=UTC)
    assert clock.latest_closed_bar_close == datetime(2026, 9, 22, 16, 0, tzinfo=UTC)
    assert clock.within_grace is False  # 16:00 closed 7.5 minutes ago
    assert clock.freshness_floor == clock.latest_closed_bar_open
    just_after = bar_clock(datetime(2026, 9, 22, 16, 0, 30, tzinfo=UTC), "M15")
    assert just_after is not None
    assert just_after.latest_closed_bar_open == datetime(2026, 9, 22, 15, 45, tzinfo=UTC)
    assert just_after.within_grace is True
    assert just_after.freshness_floor == datetime(2026, 9, 22, 15, 30, tzinfo=UTC)
    assert bar_clock(NOW, "banana") is None


def test_bar_clock_matches_the_worked_example_1907_m15() -> None:
    """19:07 UTC, M15: the latest closed bar opened 18:45 and closed 19:00 (MT5 index 1)."""
    clock = bar_clock(datetime(2026, 9, 22, 19, 7, tzinfo=UTC), "M15")
    assert clock is not None
    assert clock.latest_closed_bar_open == datetime(2026, 9, 22, 18, 45, tzinfo=UTC)
    assert clock.latest_closed_bar_close == datetime(2026, 9, 22, 19, 0, tzinfo=UTC)
    assert clock.seconds_since_close == pytest.approx(420.0)  # 7 minutes


# ------------------------------------------------------------- fresh / stale / unknown


def test_fresh_data_with_no_setup_is_healthy_waiting() -> None:
    payload = build_runtime_heartbeat(snapshot(), risk_snapshot=risk({"checks": 0}), now=NOW)
    assert payload["read_only"] is True
    assert payload["state"] == "running"
    assert payload["data_fresh"] is True
    assert payload["last_processed_bar"] == "2026-09-22T15:45:00+00:00"
    assert payload["last_processed_bar_close"] == "2026-09-22T16:00:00+00:00"
    assert payload["latest_closed_bar_open"] == "2026-09-22T15:45:00+00:00"
    assert payload["latest_closed_bar_close"] == "2026-09-22T16:00:00+00:00"
    assert payload["lag_bars"] == 0.0
    # age is measured from the bar's CLOSE (16:00), not from its open (15:45)
    assert payload["bar_age_seconds"] == pytest.approx(450.0, abs=0.01)
    assert payload["bar_open_age_seconds"] == pytest.approx(1350.0, abs=0.01)
    assert "OPEN time" in payload["bar_convention"]
    assert payload["pipeline_stage"] == "candle_evaluated_no_setup"
    assert payload["assessment"] == "healthy_waiting_for_setup"
    assert payload["cycles"] == 162 and payload["bars_processed"] == 3
    assert payload["execution"] == {"blocked": False, "trade_allowed": True, "reason": ""}


def test_freshness_follows_the_latest_closed_candle_not_recency() -> None:
    """The 19:07 example: processed bar 18:45 (closed 19:00) => age 7 min, fresh, waiting."""
    payload = build_runtime_heartbeat(
        snapshot(last_processed_bar="2026-09-22T18:45:00+00:00"),
        risk_snapshot=risk({"checks": 0}),
        now=datetime(2026, 9, 22, 19, 7, tzinfo=UTC),
    )
    assert payload["latest_closed_bar_open"] == "2026-09-22T18:45:00+00:00"
    assert payload["latest_closed_bar_close"] == "2026-09-22T19:00:00+00:00"
    assert payload["last_processed_bar_close"] == "2026-09-22T19:00:00+00:00"
    assert payload["bar_age_seconds"] == pytest.approx(420.0, abs=0.01)  # 7 minutes
    assert payload["lag_bars"] == 0.0
    assert payload["data_fresh"] is True
    assert payload["assessment"] == "healthy_waiting_for_setup"


def test_a_stuck_candle_is_reported_as_stale_not_as_waiting() -> None:
    payload = build_runtime_heartbeat(
        snapshot(last_processed_bar="2026-09-22T15:00:00+00:00"),
        risk_snapshot=risk({"checks": 0}),
        now=NOW,
    )
    assert payload["data_fresh"] is False
    assert payload["lag_bars"] == pytest.approx(3.0)  # three M15 bars behind the expected close
    assert payload["bar_age_seconds"] == pytest.approx(3150.0, abs=0.01)  # since its 15:15 close
    assert payload["assessment"] == "stale_data"


def test_grace_window_keeps_the_previous_bar_acceptable_just_after_a_close() -> None:
    payload = build_runtime_heartbeat(
        snapshot(last_processed_bar="2026-09-22T15:30:00+00:00"),
        risk_snapshot=risk({"checks": 0}),
        now=datetime(2026, 9, 22, 16, 0, 30, tzinfo=UTC),
    )
    assert payload["data_fresh"] is True  # the 16:00 close is 30s old; the loop has not polled yet
    assert payload["lag_bars"] == pytest.approx(1.0)
    assert payload["bar_age_seconds"] == pytest.approx(930.0, abs=0.01)  # since the 15:45 close
    assert any("grace" in note for note in payload["notes"])
    assert payload["assessment"] == "healthy_waiting_for_setup"


def test_stopped_runtime_does_not_claim_health_or_staleness() -> None:
    payload = build_runtime_heartbeat(snapshot(state="stopped"), now=NOW)
    assert payload["data_fresh"] is None, "a stopped runtime is neither fresh nor stale"
    assert payload["assessment"] == "not_running"
    assert any("not running" in note for note in payload["notes"])


def test_running_without_a_processed_bar_reports_unknown() -> None:
    payload = build_runtime_heartbeat(snapshot(last_processed_bar=None), now=NOW)
    assert payload["last_processed_bar"] is None
    assert payload["data_fresh"] is None
    assert payload["bar_age_seconds"] is None
    assert payload["assessment"] == "unknown"
    assert any("no processed bar yet" in note for note in payload["notes"])


def test_unknown_timeframe_is_reported_rather_than_guessed() -> None:
    payload = build_runtime_heartbeat(snapshot(timeframe="banana"), now=NOW)
    assert payload["latest_closed_bar_open"] is None
    assert payload["latest_closed_bar_close"] is None
    assert payload["data_fresh"] is None
    assert payload["assessment"] == "unknown"
    assert any("freshness not computed" in note for note in payload["notes"])


def test_execution_block_is_surfaced_above_setup_waiting() -> None:
    payload = build_runtime_heartbeat(snapshot(blocked=True), now=NOW)
    assert payload["assessment"] == "execution_blocked"
    assert payload["execution"]["blocked"] is True


def test_risk_rejections_are_named_when_the_strategy_saw_setups() -> None:
    payload = build_runtime_heartbeat(
        snapshot(generated=2, rejected=0),
        risk_snapshot=risk({"checks": 2, "approved": 0, "rejected": 2}),
        now=NOW,
    )
    assert payload["assessment"] == "setups_rejected_by_risk"
    assert payload["risk"]["rejected"] == 2
    assert payload["signal_engine"]["generated"] == 2


def test_generated_signals_with_no_orders_are_not_called_waiting() -> None:
    payload = build_runtime_heartbeat(snapshot(generated=3, orders_sent=0), now=NOW)
    assert payload["assessment"] == "healthy"


def test_builder_is_observational_and_does_not_mutate_its_input() -> None:
    source = snapshot()
    before = repr(source)
    payload = build_runtime_heartbeat(source, risk_snapshot=risk({"checks": 1}), now=NOW)
    assert repr(source) == before, "the builder must not mutate the runtime snapshot"
    assert payload["read_only"] is True
    assert "read_only" not in source
    assert payload["risk"]["checks"] == 1
    assert payload["errors"] == 0


def test_missing_counters_are_null_rather_than_zero() -> None:
    bare = {"state": "running", "symbol": "XAUUSDM", "timeframe": "M15", "stats": {}}
    payload = build_runtime_heartbeat(bare, now=NOW)
    assert payload["cycles"] is None
    assert payload["bars_processed"] is None
    assert payload["risk"]["checks"] is None
    assert payload["execution"]["blocked"] is None
    assert payload["data_fresh"] is None  # no processed bar to judge


# ---------------------------------------------------------------------- the endpoint


def _client(settings: Settings):
    app = create_app(settings)
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_heartbeat_endpoint_requires_the_bearer_token() -> None:
    settings = Settings(
        trading_mode=TradingMode.DEMO, api_token="test-token", api_host="127.0.0.1"
    )
    _app, client = _client(settings)
    token = {"Authorization": "Bearer test-token"}

    async def run() -> None:
        async with client:
            assert (await client.get("/api/v1/runtime/heartbeat")).status_code == 401
            wrong = await client.get(
                "/api/v1/runtime/heartbeat", headers={"Authorization": "Bearer nope"}
            )
            assert wrong.status_code == 401

            ok = await client.get("/api/v1/runtime/heartbeat", headers=token)
            assert ok.status_code == 200
            body = ok.json()
            assert body["read_only"] is True
            assert body["state"] == "stopped"  # a fresh app has not started the runtime
            assert body["assessment"] == "not_running"
            assert body["data_fresh"] is None

            # observational: read-only route, and the runtime state is unchanged afterwards
            posted = await client.post("/api/v1/runtime/heartbeat", headers=token)
            assert posted.status_code == 405
            after = await client.get("/api/v1/runtime/heartbeat", headers=token)
            assert after.json()["state"] == "stopped"

    asyncio.run(run())


def test_heartbeat_is_get_only_in_the_schema() -> None:
    settings = Settings(trading_mode=TradingMode.DEMO, api_token="test-token", api_host="127.0.0.1")
    app, _unused = _client(settings)
    paths = app.openapi()["paths"]
    assert "/api/v1/runtime/heartbeat" in paths
    assert set(paths["/api/v1/runtime/heartbeat"]) == {"get"}, "observational endpoint: read only"


def test_same_snapshot_yields_the_same_assessment_with_a_growing_age() -> None:
    running = snapshot()
    first = build_runtime_heartbeat(running, now=NOW)
    second = build_runtime_heartbeat(running, now=NOW + timedelta(seconds=1))
    assert first["assessment"] == second["assessment"] == "healthy_waiting_for_setup"
    assert second["bar_age_seconds"] > first["bar_age_seconds"]
    assert first["observed_at"] != second["observed_at"]



