"""Live trade recorder: excursions, exits, partials, recovery, persistence and learning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.common.enums import ExitCause, OrderSide, OutcomeSource, OutcomeStatus
from mt5_platform.common.events import PositionInfo
from mt5_platform.historical.models import HistoricalOutcome
from mt5_platform.outcomes import (
    ExcursionTracker,
    TradeOutcomeRecorder,
    cause_from_levels,
)
from mt5_platform.storage import InMemoryMarketDataStore

T0 = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
TICK = 0.001


class Clock:
    """Deterministic clock: tests advance time explicitly."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _position(
    *,
    ticket: str = "3264036722",
    side: OrderSide = OrderSide.BUY,
    volume: float = 0.04,
    entry: float = 4317.478,
    current: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    external: bool = False,
    opened_at: datetime | None = None,
    symbol: str = "XAUUSDm",
) -> PositionInfo:
    return PositionInfo(
        ticket=ticket,
        order_id=None,
        symbol=symbol,
        side=side,
        volume=volume,
        entry_price=entry,
        current_price=current if current is not None else entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        opened_at=opened_at or T0,
        magic=0 if external else 26_092_101,
        is_external=external,
    )


def _rig(**kwargs: object) -> tuple[TradeOutcomeRecorder, InMemoryMarketDataStore, Clock]:
    store = InMemoryMarketDataStore()
    clock = Clock()
    recorder = TradeOutcomeRecorder(store, clock=clock, **kwargs)  # type: ignore[arg-type]
    return recorder, store, clock


# ------------------------------------------------------------------ excursions


def test_buy_adverse_and_favorable_excursions() -> None:
    tracker = ExcursionTracker(side=OrderSide.BUY, entry=100.0)
    for price in (100.5, 99.0, 100.2, 101.5, 100.9):
        tracker.observe(price)
    assert tracker.mae == 1.0  # 100 -> 99
    assert tracker.mfe == 1.5  # 100 -> 101.5
    assert tracker.samples == 5
    assert tracker.mae_pct() == 1.0
    assert tracker.mfe_pct() == 1.5
    assert tracker.last_price == 100.9


def test_sell_adverse_and_favorable_excursions() -> None:
    tracker = ExcursionTracker(side=OrderSide.SELL, entry=100.0)
    for price in (99.4, 101.25, 100.1, 98.0, 99.9):
        tracker.observe(price)
    assert tracker.mae == 1.25  # adverse for a short is price up
    assert tracker.mfe == 2.0  # favourable is price down
    assert tracker.extreme_adverse_price == 101.25
    assert tracker.extreme_favorable_price == 98.0


def test_multiple_excursions_and_exit_after_new_maximum() -> None:
    tracker = ExcursionTracker(side=OrderSide.BUY, entry=100.0)
    tracker.observe(100.0)
    tracker.observe(99.5)
    tracker.observe(100.8)
    tracker.observe(99.7)
    tracker.observe(101.2)  # new favourable maximum right before the exit
    assert tracker.mae == pytest.approx(0.5)
    assert tracker.mfe == pytest.approx(1.2)


def test_zero_movement_trade_has_zero_excursions() -> None:
    tracker = ExcursionTracker(side=OrderSide.BUY, entry=2500.0)
    for _ in range(4):
        tracker.observe(2500.0)
    assert tracker.mae == 0.0 and tracker.mfe == 0.0 and tracker.samples == 4


def test_unusable_prices_are_ignored_not_invented() -> None:
    tracker = ExcursionTracker(side=OrderSide.BUY, entry=100.0)
    tracker.observe(None)
    tracker.observe(0.0)
    tracker.observe("nonsense")  # type: ignore[arg-type]
    assert tracker.samples == 0 and tracker.last_price is None


def test_high_low_observation_is_direction_aware() -> None:
    buy = ExcursionTracker(side=OrderSide.BUY, entry=100.0)
    buy.observe_high_low(101.0, 99.0)
    assert (buy.mfe, buy.mae) == (1.0, 1.0)
    sell = ExcursionTracker(side=OrderSide.SELL, entry=100.0)
    sell.observe_high_low(101.0, 99.0)
    assert (sell.mfe, sell.mae) == (1.0, 1.0)


# --------------------------------------------------------- exit-cause taxonomy


def test_cause_from_levels_matches_stop_and_target_only() -> None:
    cause, source = cause_from_levels(
        exit_price=2495.0, stop_loss=2495.0, take_profit=2510.0, tolerance=0.002
    )
    assert cause is ExitCause.STOP_LOSS and source == "level_match"
    cause, source = cause_from_levels(
        exit_price=2510.001, stop_loss=2495.0, take_profit=2510.0, tolerance=0.002
    )
    assert cause is ExitCause.TAKE_PROFIT and source == "level_match"
    cause, source = cause_from_levels(
        exit_price=2502.0, stop_loss=2495.0, take_profit=2510.0, tolerance=0.002
    )
    assert cause is ExitCause.UNKNOWN and source == "external_unknown"


def test_cause_from_levels_never_uses_profit_or_loss() -> None:
    """A losing exit that matches neither level stays unknown: no guessing from P/L."""
    cause, source = cause_from_levels(
        exit_price=2490.0, stop_loss=2495.0, take_profit=2510.0, tolerance=0.002
    )
    assert cause is ExitCause.UNKNOWN and source == "external_unknown"


def test_unknown_tolerance_means_unknown_cause() -> None:
    cause, source = cause_from_levels(
        exit_price=2495.0, stop_loss=2495.0, take_profit=None, tolerance=None
    )
    assert cause is ExitCause.UNKNOWN and source == "external_unknown"


# -------------------------------------------------------------- recorder lifecycle


async def test_position_opens_an_outcome_with_frozen_entry_knowledge() -> None:
    recorder, _, _ = _rig()
    record = recorder.observe_position(
        _position(stop_loss=4300.0, take_profit=4350.0, current=4318.0),
        strategy="breakout",
        strategy_version="1.0.0",
        timeframe="M15",
        tick_size=TICK,
        spread_points=260.0,
        thesis={"thesis_id": "ths_1", "direction": "buy"},
        thesis_id="ths_1",
        risk_decision={"approved": True, "reasons": []},
        evidence={"evidence_quality": "weak", "thesis_confidence": 0.4},
    )
    assert record is not None
    assert record.status is OutcomeStatus.OPEN and record.is_open
    assert record.trade_id == "trd_3264036722" and record.broker_ticket == "3264036722"
    assert record.source is OutcomeSource.AUTONOMOUS
    assert record.initial_stop_loss == 4300.0 and record.initial_take_profit == 4350.0
    assert record.entry_volume == 0.04 and record.remaining_volume == 0.04
    assert record.legs and record.legs[0].kind == "entry"
    features = record.features
    assert features is not None
    assert features.strategy == "breakout" and features.strategy_version == "1.0.0"
    assert features.spread_points == 260.0
    assert features.evidence_quality == "weak" and features.thesis_confidence == 0.4
    assert features.risk_state["approved"] is True
    assert record.thesis_snapshot["thesis_id"] == "ths_1"

    snapshot = record.features.model_dump()  # entry knowledge is frozen
    recorder.observe_price(record.broker_ticket or "", 4400.0)
    assert record.features.model_dump() == snapshot


async def test_external_manual_position_is_recorded_as_such() -> None:
    recorder, _, _ = _rig()
    record = recorder.observe_position(_position(external=True))
    assert record is not None and record.source is OutcomeSource.EXTERNAL


async def test_unknown_features_stay_explicitly_unknown() -> None:
    recorder, _, _ = _rig()
    record = recorder.observe_position(_position())  # no context, no thesis, no risk decision
    assert record is not None
    features = record.features
    assert features is not None
    assert features.regime is None and features.volatility_atr is None
    assert features.session == "" and features.evidence_quality == ""
    assert record.regime_snapshot["source"] == "unavailable"
    assert record.evidence["realized_pnl_source"] == "unavailable"


async def test_mae_and_mfe_are_tracked_then_frozen_on_close() -> None:
    recorder, _, clock = _rig()
    key = "3264036722"
    record = recorder.observe_position(_position(stop_loss=4310.0), tick_size=TICK)
    assert record is not None
    for price in (4320.0, 4312.5, 4330.0, 4315.0):
        recorder.observe_price(key, price)
    clock.advance(1800)

    frozen = recorder.finalize(
        key, exit_price=4330.0, cause=ExitCause.TAKE_PROFIT, realized_pnl=25.0, when=clock()
    )
    assert frozen is not None
    assert frozen.mae == pytest.approx(4317.478 - 4312.5)
    assert frozen.mfe == pytest.approx(4330.0 - 4317.478)
    assert frozen.mae_pct == pytest.approx((4317.478 - 4312.5) / 4317.478 * 100.0)
    assert frozen.excursion_samples == 6  # entry price + 4 observations + the exit price
    assert frozen.status is OutcomeStatus.CLOSED and frozen.is_closed
    assert frozen.duration_s == 1800.0
    assert frozen.exit_cause is ExitCause.TAKE_PROFIT
    assert frozen.exit_cause_source == "runtime"
    assert frozen.realized_pnl == 25.0
    assert frozen.evidence["realized_pnl_source"] == "legs"
    assert frozen.r_multiple == pytest.approx((4330.0 - 4317.478) / (4317.478 - 4310.0))
    assert frozen.legs[-1].kind == "exit"
    assert frozen.legs[-1].exit_cause is ExitCause.TAKE_PROFIT

    recorder.observe_price(key, 4500.0)  # frozen: later observations change nothing
    assert frozen.mfe == pytest.approx(4330.0 - 4317.478)


async def test_finalize_is_idempotent_and_never_duplicates_a_record() -> None:
    recorder, store, _ = _rig()
    key = "3264036722"
    recorder.observe_position(_position(), tick_size=TICK)
    first = recorder.finalize(
        key, exit_price=4330.0, cause=ExitCause.TAKE_PROFIT, realized_pnl=10.0
    )
    second = recorder.finalize(
        key, exit_price=4200.0, cause=ExitCause.STOP_LOSS, realized_pnl=-50.0
    )
    assert second is first and first is not None
    assert first.exit_price == 4330.0  # the second call is a no-op
    assert recorder.stats.duplicates_ignored == 1

    again = recorder.observe_position(_position())  # reconciliation sees it again
    assert again is first
    assert recorder.stats.opened == 1 and recorder.stats.closed == 1
    await recorder.flush()
    assert await store.count_outcomes() == 1


async def test_finalize_never_invents_an_outcome_for_an_unseen_position() -> None:
    recorder, _, _ = _rig()
    assert recorder.finalize("not-a-ticket", exit_price=100.0) is None


# ----------------------------------------------------------------- partial closes


async def test_partial_close_is_a_leg_and_does_not_finalize_the_trade() -> None:
    recorder, _, clock = _rig()
    key = "3264036722"
    record = recorder.observe_position(_position(volume=0.10, stop_loss=4300.0), tick_size=TICK)
    assert record is not None
    clock.advance(600)
    leg = recorder.record_partial_close(
        key, volume=0.04, price=4330.0, realized_pnl=5.0, when=clock()
    )
    assert leg is not None and leg.kind == "partial_exit"
    assert record.status is OutcomeStatus.OPEN  # NOT finalized by a partial exit
    assert record.entry_volume == 0.10
    assert record.remaining_volume == pytest.approx(0.06)
    assert record.evidence["realized_pnl_source"] == "legs"

    clock.advance(600)
    final = recorder.finalize(
        key, exit_price=4300.0, cause=ExitCause.STOP_LOSS, realized_pnl=-3.0, when=clock()
    )
    assert final is not None and final.status is OutcomeStatus.CLOSED
    assert final.remaining_volume == 0.0
    assert [item.kind for item in final.legs] == ["entry", "partial_exit", "exit"]
    assert final.realized_pnl == pytest.approx(2.0)  # partial +5.0 and final -3.0, summed
    assert final.duration_s == 1200.0
    assert final.exit_cause is ExitCause.STOP_LOSS


async def test_partial_close_volume_cannot_exceed_what_is_left() -> None:
    recorder, _, _ = _rig()
    key = "1"
    record = recorder.observe_position(_position(ticket=key, volume=0.05), tick_size=TICK)
    assert record is not None
    leg = recorder.record_partial_close(key, volume=10.0, price=4330.0)
    assert leg is not None and leg.volume == pytest.approx(0.05)
    assert record.remaining_volume == 0.0
    assert recorder.record_partial_close(key, volume=0.01, price=4331.0) is None


async def test_volume_reduction_observed_at_the_broker_is_recorded() -> None:
    recorder, _, _ = _rig()
    key = "3264036722"
    recorder.observe_position(_position(volume=0.10), tick_size=TICK)
    recorder.observe_position(_position(volume=0.06, current=4335.0))  # manual partial close
    record = recorder.get(key)
    assert record is not None
    assert record.remaining_volume == pytest.approx(0.06)
    assert [leg.kind for leg in record.legs] == ["entry", "partial_exit"]
    assert record.legs[-1].reason == "observed_volume_delta"
    assert record.legs[-1].realized_pnl is None  # unknown, not invented
    assert recorder.stats.partial_exits == 1


async def test_modifications_track_final_levels_without_rewriting_initial_ones() -> None:
    recorder, _, _ = _rig()
    key = "3264036722"
    record = recorder.observe_position(
        _position(stop_loss=4300.0, take_profit=4350.0), tick_size=TICK
    )
    assert record is not None
    recorder.record_modification(key, stop_loss=4317.478, reason="break even")
    recorder.record_modification(key, stop_loss=4325.0, take_profit=4360.0, reason="trail")
    assert record.stop_loss == 4300.0  # the initial level is never rewritten
    assert record.initial_stop_loss == 4300.0
    assert record.final_stop_loss == 4325.0 and record.final_take_profit == 4360.0
    assert [m["reason"] for m in record.evidence["modifications"]] == ["break even", "trail"]
    assert recorder.stats.modifications == 2

    recorder.observe_position(_position(stop_loss=4333.0, take_profit=4360.0))  # broker-side move
    assert record.final_stop_loss == 4333.0
    assert record.evidence["modifications"][-1]["reason"] == "observed_at_broker"


# -------------------------------------------------------- restart / crash recovery


async def test_resume_restores_excursions_and_finalizes_an_offline_close() -> None:
    store = InMemoryMarketDataStore()
    clock = Clock()
    key = "3264036722"
    first = TradeOutcomeRecorder(store, clock=clock)
    first.observe_position(_position(stop_loss=4310.0), tick_size=TICK)
    for price in (4320.0, 4312.5, 4331.0):
        first.observe_price(key, price)
    await first.flush()
    assert len(await store.get_open_outcomes()) == 1

    # process restart: brand new recorder, same database
    clock.advance(300)
    second = TradeOutcomeRecorder(store, clock=clock)
    assert await second.resume() == 1
    loaded = second.get(key)
    assert loaded is not None and loaded.status is OutcomeStatus.OPEN
    assert loaded.mae == pytest.approx(4317.478 - 4312.5)  # MAE/MFE survive the restart
    assert loaded.mfe == pytest.approx(4331.0 - 4317.478)

    result = second.reconcile([])  # the broker reports no positions: it closed while we were down
    assert result["finalized"] == ["trd_3264036722"]
    outcome = second.get(key)
    assert outcome is not None and outcome.status is OutcomeStatus.CLOSED
    assert outcome.exit_cause is ExitCause.UNKNOWN  # unknowable, so not invented
    assert outcome.exit_cause_source == "recovery"
    assert outcome.exit_price == pytest.approx(4331.0)  # last price we actually observed
    assert outcome.mae == pytest.approx(4317.478 - 4312.5)
    assert outcome.mfe == pytest.approx(4331.0 - 4317.478)
    await second.flush()
    assert await store.count_outcomes(status="open") == 0
    assert await store.count_outcomes(status="closed") == 1


async def test_recovered_stop_out_is_attributed_from_the_recorded_level() -> None:
    store = InMemoryMarketDataStore()
    clock = Clock()
    first = TradeOutcomeRecorder(store, clock=clock)
    first.observe_position(_position(ticket="9", stop_loss=4310.0), tick_size=TICK)
    first.observe_price("9", 4310.001)  # traded at the stop just before the restart
    await first.flush()

    clock.advance(60)
    second = TradeOutcomeRecorder(store, clock=clock)
    await second.resume()
    second.reconcile([])
    outcome = second.get("9")
    assert outcome is not None
    assert outcome.exit_cause is ExitCause.STOP_LOSS
    assert outcome.exit_cause_source == "level_match"


async def test_reconcile_keeps_tracking_positions_that_are_still_open() -> None:
    recorder, _, _ = _rig()
    recorder.observe_position(_position(stop_loss=4310.0), tick_size=TICK)
    result = recorder.reconcile(
        [_position(stop_loss=4310.0, current=4325.0)], tick_sizes={"XAUUSDm": TICK}
    )
    assert result["finalized"] == [] and result["open_outcomes"] == 1
    record = recorder.get("3264036722")
    assert record is not None and record.status is OutcomeStatus.OPEN
    assert record.mfe == pytest.approx(4325.0 - 4317.478)


async def test_reconcile_finalizes_a_position_closed_while_the_loop_was_running() -> None:
    recorder, store, clock = _rig()
    key = "3264036722"
    recorder.observe_position(_position(stop_loss=4310.0), tick_size=TICK)
    recorder.observe_price(key, 4332.0)
    clock.advance(420)
    result = recorder.reconcile([], tick_sizes={"XAUUSDm": TICK})
    assert result["finalized"] == ["trd_3264036722"]
    outcome = recorder.get(key)
    assert outcome is not None
    assert outcome.exit_cause_source == "external"  # not a restart: closed live at the broker
    assert outcome.exit_cause is ExitCause.UNKNOWN
    await recorder.flush()
    assert await store.count_outcomes() == 1
    assert recorder.stats.orphans_finalized == 1


async def test_recovered_closed_ticket_is_never_reopened() -> None:
    store = InMemoryMarketDataStore()
    clock = Clock()
    key = "3264036722"
    first = TradeOutcomeRecorder(store, clock=clock)
    first.observe_position(_position(stop_loss=4310.0), tick_size=TICK)
    first.finalize(key, exit_price=4330.0, cause=ExitCause.TAKE_PROFIT, realized_pnl=1.0)
    await first.flush()

    second = TradeOutcomeRecorder(store, clock=clock)
    await second.resume()
    assert second.observe_position(_position(stop_loss=4310.0)) is None  # duplicate ignored
    assert second.stats.duplicates_ignored == 1
    assert second.stats.opened == 0


# ------------------------------------------------------- persistence and recovery


class _FlakyStore(InMemoryMarketDataStore):
    """Simulates a database that is temporarily unavailable."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = 0

    async def write_outcome(self, outcome: HistoricalOutcome) -> None:  # type: ignore[override]
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("database unavailable")
        await super().write_outcome(outcome)


async def test_persistence_failure_is_queued_retried_and_never_raises() -> None:
    store = _FlakyStore()
    recorder = TradeOutcomeRecorder(store, clock=Clock())
    recorder.observe_position(_position(), tick_size=TICK)
    store.fail_next = 1
    assert await recorder.flush() == 0  # the failure is swallowed, nothing persisted
    assert recorder.stats.persist_failures == 1
    assert recorder.stats.pending_persist == 1
    assert recorder.stats.last_error is not None
    assert await store.count_outcomes() == 0
    assert recorder.get("3264036722") is not None  # the in-memory record is preserved

    assert await recorder.flush() == 1  # retry once the database recovers
    assert recorder.stats.persist_retried == 1
    assert recorder.stats.pending_persist == 0
    assert await store.count_outcomes() == 1


async def test_flush_keeps_a_failed_close_queued_for_retry() -> None:
    store = _FlakyStore()
    recorder = TradeOutcomeRecorder(store, clock=Clock())
    key = "1"
    recorder.observe_position(_position(ticket=key, stop_loss=4310.0), tick_size=TICK)
    recorder.finalize(key, exit_price=4330.0, cause=ExitCause.TAKE_PROFIT, realized_pnl=2.0)
    store.fail_next = 2
    await recorder.flush()
    assert recorder.stats.persist_failures >= 1
    assert await store.count_outcomes() == 0
    await recorder.flush()
    await recorder.flush()
    rows = await store.get_outcomes(limit=5)
    assert len(rows) == 1 and rows[0].status is OutcomeStatus.CLOSED


async def test_recorder_without_a_store_keeps_records_in_memory_only() -> None:
    recorder = TradeOutcomeRecorder(None, clock=Clock())
    recorder.observe_position(_position())
    assert await recorder.flush() == 0
    assert recorder.stats.storage_available is False
    assert recorder.summary()["open"] == 1  # trading continues; recording stays in memory


# ------------------------------------------------------------ deterministic replay


async def test_replay_of_the_same_path_is_deterministic() -> None:
    path = [4318.0, 4312.5, 4330.0, 4322.0, 4341.75]
    results: list[tuple[object, ...]] = []
    for _ in range(2):
        recorder, _, clock = _rig()
        key = "777"
        recorder.observe_position(
            _position(ticket=key, stop_loss=4300.0, take_profit=4340.0), tick_size=TICK
        )
        for price in path:
            clock.advance(300)
            recorder.observe_price(key, price)
        clock.advance(300)
        recorder.finalize(
            key,
            exit_price=path[-1],
            cause=ExitCause.TAKE_PROFIT,
            realized_pnl=12.5,
            when=clock(),
        )
        record = recorder.get(key)
        assert record is not None
        results.append(
            (
                record.entry,
                record.exit_price,
                record.mae,
                record.mfe,
                record.realized_pnl,
                record.return_pct,
                record.r_multiple,
                record.duration_s,
                record.exit_cause,
                record.excursion_samples,
            )
        )
    assert results[0] == results[1]


# --------------------------------------------------------- round trip + summary


async def test_outcome_round_trip_through_the_memory_store() -> None:
    recorder, store, clock = _rig()
    key = "3264036722"
    recorder.observe_position(
        _position(stop_loss=4300.0, take_profit=4350.0),
        strategy="breakout",
        timeframe="M15",
        tick_size=TICK,
    )
    recorder.observe_price(key, 4330.0)
    clock.advance(900)
    recorder.finalize(
        key, exit_price=4350.0, cause=ExitCause.TAKE_PROFIT, realized_pnl=30.0, when=clock()
    )
    await recorder.flush()

    rows = await store.get_outcomes(limit=10)
    assert len(rows) == 1
    assert await store.count_outcomes(status="closed") == 1
    assert await store.get_open_outcomes() == []
    assert len(await store.get_outcomes(symbol="XAUUSDm", strategy="breakout")) == 1
    assert await store.get_outcomes(symbol="EURUSD") == []

    original = recorder.get(key)
    assert original is not None and rows[0].model_dump() == original.model_dump()


async def test_sqlite_schema_persists_outcomes_losslessly_and_idempotently() -> None:
    from mt5_platform.storage.db import create_engine, create_session_factory, init_db
    from mt5_platform.storage.sqlalchemy_store import SqlAlchemyMarketDataStore

    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)  # creates the outcomes and trade_legs tables
    store = SqlAlchemyMarketDataStore(create_session_factory(engine))
    recorder = TradeOutcomeRecorder(store, clock=Clock())
    key = "3264036722"
    recorder.observe_position(
        _position(stop_loss=4300.0, take_profit=4350.0),
        strategy="breakout",
        strategy_version="1.0.0",
        timeframe="M15",
        tick_size=TICK,
    )
    recorder.observe_price(key, 4330.0)
    recorder.record_partial_close(key, volume=0.02, price=4330.0, realized_pnl=3.0)
    await recorder.flush()

    open_rows = await store.get_open_outcomes()
    assert len(open_rows) == 1
    assert len(open_rows[0].legs) == 2  # entry + partial exit survived the database

    recorder.finalize(key, exit_price=4350.0, cause=ExitCause.TAKE_PROFIT, realized_pnl=7.0)
    await recorder.flush()
    await recorder.flush()  # idempotent: still exactly one row
    assert await store.count_outcomes() == 1
    assert await store.count_outcomes(status="closed") == 1

    closed = (await store.get_outcomes(limit=5))[0]
    stored = recorder.get(key)
    assert stored is not None
    assert closed.model_dump() == stored.model_dump()  # lossless round trip
    assert closed.exit_cause is ExitCause.TAKE_PROFIT
    assert closed.realized_pnl == pytest.approx(10.0)  # 3.0 partial + 7.0 final
    assert closed.r_multiple is not None
    assert [leg.kind for leg in closed.legs] == ["entry", "partial_exit", "exit"]
    await engine.dispose()


async def test_summary_separates_populations_and_reports_availability() -> None:
    recorder, _, clock = _rig()
    recorder.observe_position(_position(ticket="1", stop_loss=4310.0), tick_size=TICK)
    recorder.observe_position(_position(ticket="2", external=True), tick_size=TICK)
    clock.advance(60)
    recorder.finalize(
        "1", exit_price=4330.0, cause=ExitCause.TAKE_PROFIT, realized_pnl=8.0, when=clock()
    )

    summary = recorder.summary()
    assert summary["open"] == 1 and summary["completed"] == 1
    assert summary["autonomous"] == 1 and summary["external"] == 0 and summary["backtest"] == 0
    assert summary["by_exit_cause"] == {"take_profit": 1}
    assert summary["mae_mfe_available"] == 1
    assert summary["r_multiple_available"] == 1
    assert summary["realized_pnl_available"] == 1
    assert summary["with_partial_exits"] == 0
    last = summary["last_completed"]
    assert last["exit_cause"] == "take_profit" and last["ticket"] == "1"
    assert last["source"] == "autonomous"
    assert summary["recorder"]["opened"] == 2 and summary["recorder"]["closed"] == 1
