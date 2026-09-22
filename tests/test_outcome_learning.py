"""Backtest/live outcome schema compatibility, learning integration and evidence quality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mt5_platform.backtest.engine import Trade as BacktestTrade
from mt5_platform.backtest.outcomes import (
    exit_cause_from_backtest,
    outcome_from_backtest_trade,
)
from mt5_platform.common.enums import (
    EvidenceQuality,
    ExitCause,
    LessonStatus,
    OrderSide,
    OutcomeSource,
)
from mt5_platform.common.events import PositionInfo
from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.models import HistoricalOutcome
from mt5_platform.historical.outcome_loader import (
    DEFAULT_MIN_MODERATE,
    DEFAULT_MIN_STRONG,
    DEFAULT_MIN_WEAK,
    evidence_status,
    load_live_outcomes,
)
from mt5_platform.learning import (
    DecisionMemory,
    HypothesisRegistry,
    PostTradeReviewEngine,
)
from mt5_platform.outcomes import OutcomeLearningPipeline, TradeOutcomeRecorder, to_trade_outcome
from mt5_platform.storage import InMemoryMarketDataStore

T0 = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _backtest_trade(
    *,
    side: OrderSide = OrderSide.BUY,
    entry: float = 2500.0,
    exit_price: float = 2510.0,
    pnl: float = 10.0,
    r_multiple: float = 1.0,
    reason: str = "tp",
    minutes: int = 30,
) -> BacktestTrade:
    return BacktestTrade(
        strategy="donchian_20",
        side=side,
        volume=0.01,
        entry=entry,
        exit=exit_price,
        stop_loss=2490.0,
        opened_at=T0,
        closed_at=T0 + timedelta(minutes=minutes),
        pnl=pnl,
        r_multiple=r_multiple,
        exit_reason=reason,
    )


# --------------------------------------------------------- backtest compatibility


def test_backtest_trade_normalizes_into_the_live_outcome_schema() -> None:
    record = outcome_from_backtest_trade(
        _backtest_trade(), symbol="XAUUSDm", timeframe="M15", strategy_version="1.0.0"
    )
    assert isinstance(record, HistoricalOutcome)
    assert record.source is OutcomeSource.BACKTEST
    assert record.status.value == "closed"
    assert record.instrument == "XAUUSDm"  # broker case preserved
    assert record.strategy == "donchian_20" and record.strategy_version == "1.0.0"
    assert record.exit_cause is ExitCause.TAKE_PROFIT and record.exit_cause_source == "backtest"
    assert record.exit_price == 2510.0 and record.realized_pnl == 10.0
    assert record.r_multiple == 1.0
    assert record.duration_s == 1800.0
    assert record.entry_volume == 0.01
    # gaps stay explicit instead of being invented
    assert record.mae == 0.0 and record.mfe == 0.0
    assert record.evidence["excursions_available"] is False
    assert record.evidence["return_pct_source"] == "unavailable"
    assert record.realized_pnl_pct is None
    assert record.commission is None and record.swap is None


def test_backtest_exit_reasons_map_to_the_shared_taxonomy() -> None:
    assert exit_cause_from_backtest("sl") is ExitCause.STOP_LOSS
    assert exit_cause_from_backtest("tp") is ExitCause.TAKE_PROFIT
    assert exit_cause_from_backtest("end") is ExitCause.TIME_STOP
    assert exit_cause_from_backtest("something_new") is ExitCause.UNKNOWN
    assert exit_cause_from_backtest("") is ExitCause.UNKNOWN


def test_backtest_reported_return_pct_is_kept_when_supplied() -> None:
    record = outcome_from_backtest_trade(
        _backtest_trade(), symbol="XAUUSDm", return_pct=0.4
    )
    assert record.return_pct == 0.4
    assert record.realized_pnl_pct == 0.4
    assert record.evidence["return_pct_source"] == "reported"


def test_live_and_backtest_outcomes_share_the_same_fields() -> None:
    """Research can compare populations only because the schema is identical."""

    class _Result:
        def __init__(self) -> None:
            self.config = type("C", (), {"symbol": "XAUUSDm"})()
            self.trades = [
                _backtest_trade(),
                _backtest_trade(reason="sl", pnl=-5.0, r_multiple=-1.0),
            ]

    backtest = outcome_from_backtest_trade(_backtest_trade(), symbol="XAUUSDm")
    live = HistoricalOutcome(
        instrument="XAUUSDm", direction=OrderSide.BUY, timestamp=T0, entry=2500.0
    )
    for field in ("instrument", "strategy", "direction", "source", "exit_cause", "r_multiple"):
        assert hasattr(backtest, field) and hasattr(live, field)
    assert backtest.schema_version == live.schema_version
    assert len(_Result().trades) == 2


# --------------------------------------------------------------- learning wiring


async def _recorded_outcome(
    recorder: TradeOutcomeRecorder,
    clock: Clock,
    *,
    ticket: str = "42",
    external: bool = False,
    stop_loss: float = 4310.0,
    exit_price: float = 4330.0,
    pnl: float = 20.0,
    cause: ExitCause = ExitCause.TAKE_PROFIT,
    strategy: str = "breakout",
):
    position = PositionInfo(
        ticket=ticket,
        order_id=None,
        symbol="XAUUSDm",
        side=OrderSide.BUY,
        volume=0.04,
        entry_price=4317.478,
        current_price=4317.478,
        stop_loss=stop_loss,
        take_profit=None,
        opened_at=clock(),
        magic=0 if external else 26_092_101,
        is_external=external,
    )
    recorder.observe_position(
        position, strategy=strategy, strategy_version="1.0.0", timeframe="M15"
    )
    clock.advance(600)
    return recorder.finalize(
        ticket, exit_price=exit_price, cause=cause, realized_pnl=pnl, when=clock()
    )


async def test_completed_outcome_reaches_memory_review_and_hypotheses() -> None:
    memory = DecisionMemory()
    registry = HypothesisRegistry()
    pipeline = OutcomeLearningPipeline(
        memory=memory, review_engine=PostTradeReviewEngine(), hypotheses=registry
    )
    store = InMemoryMarketDataStore()
    clock = Clock()
    recorder = TradeOutcomeRecorder(store, clock=clock, on_completed=pipeline.record)

    outcome = await _recorded_outcome(recorder, clock)
    assert outcome is not None

    record = memory.get_by_trade_id(outcome.trade_id)
    assert record is not None
    assert record.review is not None and record.review.trade_id == outcome.trade_id
    assert record.outcome is not None and record.outcome.realized_pnl == 20.0
    assert len(record.position_management) == 2  # entry + exit legs
    assert pipeline.stats()["reviews"] == 1
    assert pipeline.stats()["memory_records"] == 1

    lessons = list(getattr(registry, "_hypotheses", {}).values())
    for lesson in lessons:  # any lesson proposed is an observation only, never a change
        assert lesson.status is LessonStatus.PROPOSED
        assert lesson.sample_size == 1
        assert lesson.proposed_change_type is None
        assert lesson.proposed_change == {}
    assert pipeline.stats()["lessons_proposed"] == len(lessons)

    # A large adverse excursion does propose a lesson — still without any proposed change.
    await _recorded_outcome(
        recorder,
        clock,
        ticket="43",
        stop_loss=3850.0,
        exit_price=3850.0,
        pnl=-30.0,
        cause=ExitCause.STOP_LOSS,
    )
    proposed = list(getattr(registry, "_hypotheses", {}).values())
    assert len(proposed) > len(lessons)
    assert all(lesson.proposed_change == {} for lesson in proposed)
    assert all(lesson.proposed_change_type is None for lesson in proposed)


async def test_learning_never_touches_configuration_state() -> None:
    """Nothing in the learning path may mutate live trading configuration."""
    from mt5_platform.config import Settings

    before = Settings()
    memory = DecisionMemory()
    pipeline = OutcomeLearningPipeline(memory=memory)
    store = InMemoryMarketDataStore()
    clock = Clock()
    recorder = TradeOutcomeRecorder(store, clock=clock, on_completed=pipeline.record)
    await _recorded_outcome(recorder, clock, cause=ExitCause.STOP_LOSS, exit_price=4310.0, pnl=-5.0)

    after = Settings()
    assert before.model_dump() == after.model_dump()
    assert memory.all(), "the review is still recorded"
    assert pipeline.errors == 0


async def test_intelligence_record_completed_trade_is_called() -> None:
    calls: list[dict] = []

    class _Intelligence:
        def record_completed_trade(self, **kwargs):
            calls.append(kwargs)

    pipeline = OutcomeLearningPipeline()
    store = InMemoryMarketDataStore()
    clock = Clock()
    recorder = TradeOutcomeRecorder(
        store, clock=clock, on_completed=lambda o: pipeline.record(o, intelligence=_Intelligence())
    )
    outcome = await _recorded_outcome(recorder, clock)
    assert outcome is not None
    assert len(calls) == 1
    assert calls[0]["pnl"] == 20.0
    assert calls[0]["signal"].symbol == "XAUUSDm"
    assert calls[0]["signal"].strategy_name == "breakout"


async def test_learning_failure_never_breaks_recording_or_trading() -> None:
    def _explode(outcome):  # pragma: no cover - exercised through the recorder
        raise RuntimeError("learning layer down")

    store = InMemoryMarketDataStore()
    clock = Clock()
    recorder = TradeOutcomeRecorder(store, clock=clock, on_completed=_explode)
    outcome = await _recorded_outcome(recorder, clock)
    assert outcome is not None and outcome.status.value == "closed"
    assert recorder.stats.last_error is not None
    assert "completion hook failed" in recorder.stats.last_error
    await recorder.flush()
    assert await store.count_outcomes() == 1  # the record itself survived


async def test_manual_position_outcome_is_kept_in_its_own_population() -> None:
    memory = DecisionMemory()
    pipeline = OutcomeLearningPipeline(memory=memory)
    store = InMemoryMarketDataStore()
    clock = Clock()
    recorder = TradeOutcomeRecorder(store, clock=clock, on_completed=pipeline.record)
    outcome = await _recorded_outcome(recorder, clock, ticket="9", external=True)
    assert outcome is not None and outcome.source is OutcomeSource.EXTERNAL
    recorded = memory.get_by_trade_id(outcome.trade_id)
    assert recorded is not None
    assert to_trade_outcome(outcome).instrument == "XAUUSDm"


# --------------------------------------------------------------- evidence quality


def _outcome(
    index: int,
    *,
    pnl: float = 1.0,
    instrument: str = "XAUUSDm",
    source: OutcomeSource = OutcomeSource.AUTONOMOUS,
    exit_price: float | None = 2510.0,
) -> HistoricalOutcome:
    return HistoricalOutcome(
        instrument=instrument,
        direction=OrderSide.BUY,
        source=source,
        timestamp=T0 + timedelta(minutes=index),
        entry=2500.0,
        exit_price=exit_price,
        exit_time=(T0 + timedelta(minutes=index + 30)) if exit_price is not None else None,
        realized_pnl=pnl,
        return_pct=pnl,
    )


def test_evidence_quality_progresses_with_real_outcomes() -> None:
    ledger = InMemoryHistoricalLedger()
    empty = evidence_status(ledger, instrument="XAUUSDm")
    assert empty["sample_size"] == 0
    assert empty["evidence_quality"] == EvidenceQuality.INSUFFICIENT.value
    assert empty["minimum_sample_required"] == DEFAULT_MIN_WEAK == 10
    assert empty["thresholds_lowered"] is False

    ledger.record_many([_outcome(i) for i in range(4)])
    small = evidence_status(ledger, instrument="XAUUSDm")
    assert small["sample_size"] == 4
    assert small["evidence_quality"] == EvidenceQuality.INSUFFICIENT.value

    ledger.record_many([_outcome(i) for i in range(4, 10)])
    weak = evidence_status(ledger, instrument="XAUUSDm")
    assert weak["sample_size"] == 10
    assert weak["evidence_quality"] == EvidenceQuality.WEAK.value

    ledger.record_many([_outcome(i) for i in range(10, 30)])
    moderate = evidence_status(ledger, instrument="XAUUSDm")
    assert moderate["sample_size"] == 30
    assert moderate["evidence_quality"] == EvidenceQuality.MODERATE.value

    ledger.record_many([_outcome(i) for i in range(30, 100)])
    strong = evidence_status(ledger, instrument="XAUUSDm")
    assert strong["sample_size"] == 100
    assert strong["evidence_quality"] == EvidenceQuality.STRONG.value
    # The thresholds are the engine's own defaults: reported, never lowered.
    thresholds = (
        strong["min_sample_weak"],
        strong["min_sample_moderate"],
        strong["min_sample_strong"],
    )
    assert thresholds == (DEFAULT_MIN_WEAK, DEFAULT_MIN_MODERATE, DEFAULT_MIN_STRONG)


def test_open_outcomes_are_not_counted_as_evidence() -> None:
    ledger = InMemoryHistoricalLedger()
    ledger.record(_outcome(1, exit_price=None))
    status = evidence_status(ledger, instrument="XAUUSDm")
    assert status["available_outcomes"] == 1
    assert status["sample_size"] == 0
    assert status["open_outcomes"] == 1


def test_evidence_is_scoped_to_the_exact_instrument_case() -> None:
    ledger = InMemoryHistoricalLedger()
    ledger.record_many([_outcome(i) for i in range(12)])
    assert evidence_status(ledger, instrument="XAUUSDm")["sample_size"] == 12
    assert evidence_status(ledger, instrument="XAUUSDM")["sample_size"] == 0  # never conflated


async def test_ledger_loads_only_live_populations_from_the_store() -> None:
    store = InMemoryMarketDataStore()
    await store.write_outcome(_outcome(1))
    await store.write_outcome(_outcome(2, source=OutcomeSource.EXTERNAL))
    await store.write_outcome(_outcome(3, source=OutcomeSource.BACKTEST))

    loaded = await load_live_outcomes(store)
    assert len(loaded) == 2
    assert {row.source for row in loaded} == {OutcomeSource.AUTONOMOUS, OutcomeSource.EXTERNAL}

    ledger = InMemoryHistoricalLedger()
    ledger.record_many(loaded)
    status = evidence_status(ledger, instrument="XAUUSDm")
    assert status["sample_size"] == 2
    assert status["sources"] == {"autonomous": 1, "external": 1}
    assert status["backtest_excluded"] is True


async def test_recorder_persisted_outcomes_feed_the_evidence_ledger() -> None:
    """End of the chain: recorder -> store -> ledger -> EvidenceEngine sample count."""
    store = InMemoryMarketDataStore()
    clock = Clock()
    recorder = TradeOutcomeRecorder(store, clock=clock)
    for index in range(3):
        await _recorded_outcome(recorder, clock, ticket=str(100 + index), pnl=float(index + 1))
    await recorder.flush()
    assert await store.count_outcomes(status="closed") == 3

    ledger = InMemoryHistoricalLedger()
    ledger.record_many(await load_live_outcomes(store))
    status = evidence_status(ledger, instrument="XAUUSDm")
    assert status["sample_size"] == 3
    assert status["evidence_quality"] == EvidenceQuality.INSUFFICIENT.value  # honest: 3 < 10

