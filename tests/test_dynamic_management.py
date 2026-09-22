"""Phase 3: dynamic in-trade management (PositionManager -> Risk -> OrderManager -> broker).

Two things must hold at the same time:

* in-trade decisions (trailing, break-even, reductions, exits) really do reach the broker and are
  recorded as outcomes with the decision's explicit exit cause;
* the agent/synthesis entry path stays gated, so enabling management does not silently add a second
  entry source next to the strategy registry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mt5_platform.account import AccountMonitor
from mt5_platform.backtest.data import Bar
from mt5_platform.common.enums import OrderSide, OrderStatus, OutcomeStatus, PositionDecision
from mt5_platform.common.events import OrderRequest
from mt5_platform.config import Settings
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.outcomes import TradeOutcomeRecorder
from mt5_platform.position.models import PositionDecisionModel
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime import Quote, TradingLoop
from mt5_platform.runtime.intelligence import IntelligenceStats
from mt5_platform.signals import SignalEngine
from mt5_platform.storage import InMemoryMarketDataStore
from tests.fake_mt5 import FakeMT5

T0 = datetime(2024, 1, 2, 9, tzinfo=UTC)
SYMBOL = "XAUUSD"


class FakeFeed:
    """Deterministic bars/quote; `timeframe` is what the loop uses for outcome attribution."""

    timeframe = "M15"

    def __init__(self, bars: list[Bar], quote: Quote) -> None:
        self.bars, self._quote = bars, quote

    async def latest_closed_bar(self, symbol: str):
        return self.bars[-1] if self.bars else None

    async def history(self, symbol: str, count: int):
        return self.bars[-count:]

    async def quote(self, symbol: str):
        return self._quote

    def add_bar(self) -> None:
        last = self.bars[-1]
        self.bars.append(
            Bar(last.time + timedelta(minutes=15), 2500, 2501, 2499, 2500, 10, 25)
        )


def _bars(n: int = 3) -> list[Bar]:
    return [Bar(T0 + timedelta(minutes=15 * i), 2500, 2501, 2499, 2500, 10, 25) for i in range(n)]


class StubIntelligence:
    """Records what the loop asks of the intelligence layer, without running agents."""

    def __init__(self, *, signal_factory=None) -> None:
        self.stats = IntelligenceStats()
        self.calls: list[str] = []
        self.signal_factory = signal_factory
        self.positions_seen: list[int] = []

    def feed_tick(self, *, bid: float, ask: float, timestamp: datetime | None = None) -> Any:
        self.calls.append("feed_tick")
        return type("Ctx", (), {"usable_for_trading": True})()

    def produce_thesis(self, ctx: Any) -> Any:
        self.calls.append("produce_thesis")
        return object()

    def thesis_to_signal(self, thesis: Any) -> Any:
        self.calls.append("thesis_to_signal")
        return self.signal_factory() if self.signal_factory else None

    def evaluate_positions(self, positions: list[Any], ctx: Any) -> list[tuple[Any, Any]]:
        self.calls.append("evaluate_positions")
        self.positions_seen.append(len(positions))
        self.stats.position_evaluations += len(positions)
        return []


def _decision(
    action: PositionDecision,
    reason: str,
    *,
    ticket: str = "1",
    stop_loss: float | None = None,
    volume: float | None = None,
) -> PositionDecisionModel:
    return PositionDecisionModel(
        position_id=ticket,
        original_thesis_id="ths_test",
        current_context_id="ctx_test",
        decision=action,
        reason=reason,
        proposed_stop_loss=stop_loss,
        proposed_volume=volume,
    )


def _approved_order(*, side: OrderSide = OrderSide.BUY) -> OrderRequest:
    entry = 2500.3 if side is OrderSide.BUY else 2500.0
    return OrderRequest(
        symbol=SYMBOL,
        side=side,
        volume=0.01,
        entry=entry,
        stop_loss=entry - 5 if side is OrderSide.BUY else entry + 5,
        take_profit=entry + 10 if side is OrderSide.BUY else entry - 10,
        status=OrderStatus.APPROVED,
    )


def _rig(
    *,
    entries_enabled: bool = False,
    intelligence: Any | None = None,
    store: Any | None = None,
    news_ingestor: Any | None = None,
    news_blackout_enabled: bool = False,
):
    settings = Settings(
        execution_backend="mt5",
        default_symbol=SYMBOL,
        intelligence_entries_enabled=entries_enabled,
        news_blackout_enabled=news_blackout_enabled,
    )
    fake = FakeMT5(balance=10_000.0)
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    feed = FakeFeed(_bars(), Quote(2500.0, 2500.3, 30.0, 0.0))
    storage = store if store is not None else InMemoryMarketDataStore()
    recorder = TradeOutcomeRecorder(storage)
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,
        feed=feed,
        signal_engine=SignalEngine([]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=[SYMBOL],
        poll_s=0.01,
        warmup_bars=3,
        reconcile_every_s=0.0,
        intelligence=intelligence,
        outcome_recorder=recorder,
        news_ingestor=news_ingestor,
    )
    return loop, fake, adapter, recorder, storage


async def _open_position(loop, adapter, fake) -> Any:
    """Open one bot-owned position at the broker and let the loop observe it."""
    await adapter.submit_order(_approved_order())
    assert fake.positions, "the test needs an open broker position"
    positions = await loop._reconcile_positions()  # noqa: SLF001 - the loop's own observation hook
    return positions[0]


# ------------------------------------------------------- in-trade decision wiring


async def test_exit_decision_reaches_the_broker_and_freezes_the_outcome() -> None:
    loop, fake, adapter, recorder, _store = _rig()
    await loop.start()
    position = await _open_position(loop, adapter, fake)
    record = recorder.get(position.ticket)
    assert record is not None and record.status is OutcomeStatus.OPEN

    account = await adapter.get_account()
    await loop._manage_position(  # noqa: SLF001 - the exact call the intelligence path makes
        position,
        _decision(PositionDecision.EXIT, "thesis invalidated: regime change"),
        account,
    )

    assert fake.positions == [], "the in-trade exit decision reached the broker"
    outcome = recorder.get(position.ticket)
    assert outcome is not None
    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.exit_cause.value == "thesis_invalidation"
    assert outcome.exit_cause_source == "decision_reason"
    assert outcome.exit_price is not None  # from the broker's closing deal
    assert outcome.evidence["realized_pnl_source"] != "unavailable"
    assert loop.position_management["last"]["outcome"] == "executed"
    assert loop.position_management["counts"]["action:exit"] == 1


async def test_reduce_decision_is_recorded_as_a_partial_leg() -> None:
    loop, fake, adapter, recorder, _store = _rig()
    await loop.start()
    position = await _open_position(loop, adapter, fake)
    account = await adapter.get_account()

    await loop._manage_position(  # noqa: SLF001
        position,
        _decision(PositionDecision.REDUCE, "thesis weakening", volume=0.005),
        account,
    )

    record = recorder.get(position.ticket)
    assert record is not None
    assert record.status is OutcomeStatus.OPEN, "a reduction must not finalize the trade"
    assert loop.position_management["last"]["outcome"] == "executed"
    assert loop.position_management["counts"]["action:reduce"] == 1


async def test_risk_refused_position_action_is_traced_but_not_executed() -> None:
    loop, fake, adapter, recorder, _store = _rig()
    await loop.start()
    position = await _open_position(loop, adapter, fake)
    account = await adapter.get_account()
    sent_before = len(fake.sent)

    # Moving a long's stop DOWN increases risk: the risk engine must refuse it.
    await loop._manage_position(  # noqa: SLF001
        position,
        _decision(PositionDecision.MODIFY, "widen the stop", stop_loss=2480.0),
        account,
    )

    assert len(fake.sent) == sent_before, "nothing may reach the broker"
    assert loop.position_management["last"]["outcome"] == "risk_refused"
    record = recorder.get(position.ticket)
    assert record is not None and record.status is OutcomeStatus.OPEN
    assert loop.position_management["counts"]["outcome:risk_refused"] == 1


async def test_hold_decision_is_traced_without_touching_the_broker() -> None:
    loop, fake, adapter, _recorder, _store = _rig()
    await loop.start()
    position = await _open_position(loop, adapter, fake)
    account = await adapter.get_account()
    sent_before = len(fake.sent)

    await loop._manage_position(  # noqa: SLF001
        position, _decision(PositionDecision.HOLD, "thesis intact"), account
    )

    assert len(fake.sent) == sent_before
    assert loop.position_management["last"]["outcome"] == "hold"
    assert loop.position_management["last"]["action"] == "hold"


async def test_management_trace_is_exposed_in_the_runtime_stats_shape() -> None:
    stub = StubIntelligence()
    loop, fake, adapter, _recorder, _store = _rig(intelligence=stub)
    await loop.start()
    await _open_position(loop, adapter, fake)

    await loop.run_once()

    trace = loop.position_management
    assert set(trace) == {"last", "counts", "intelligence", "entries_enabled"}
    assert stub.positions_seen and stub.positions_seen[-1] == 1, "the open position is managed"
    assert trace["intelligence"]["position_evaluations"] >= 1


async def test_management_still_runs_when_entries_are_disabled_with_the_real_layer() -> None:
    """The real intelligence layer must build contexts and manage positions without new entries."""
    from mt5_platform.runtime.intelligence import IntelligenceLayer

    loop, fake, adapter, _recorder, _store = _rig(intelligence=IntelligenceLayer(symbol=SYMBOL))
    await loop.start()
    await _open_position(loop, adapter, fake)

    await loop.run_once()

    trace = loop.position_management
    assert trace["entries_enabled"] is False
    assert trace["intelligence"] is not None
    assert trace["intelligence"]["contexts_built"] >= 1, "ticks feed the context engine"
    assert loop.stats.orders_sent == 0, "no entry was created by the agents"


# --------------------------------------------------------------------- entry gate


async def test_agent_entries_are_gated_off_by_default() -> None:
    stub = StubIntelligence(
        signal_factory=lambda: (_ for _ in ()).throw(AssertionError("must not be called"))
    )
    loop, fake, adapter, _recorder, _store = _rig(intelligence=stub)
    await loop.start()
    await _open_position(loop, adapter, fake)

    await loop.run_once()  # intelligence is enabled but entry generation is not

    assert "feed_tick" in stub.calls
    assert "produce_thesis" not in stub.calls, "the agent entry path must stay closed by default"
    assert "evaluate_positions" in stub.calls, "position management always runs"
    assert stub.positions_seen[-1] == 1, "the open position was handed to the manager"
    assert loop.stats.orders_sent == 0
    assert loop.position_management["entries_enabled"] is False


async def test_entry_flag_opens_the_thesis_path_only_when_requested() -> None:
    from mt5_platform.common.events import StrategySignal

    def _signal() -> StrategySignal:
        return StrategySignal(
            symbol=SYMBOL,
            direction=OrderSide.BUY,
            entry=2500.3,
            stop_loss=2495.0,
            take_profit=2510.0,
            confidence=0.9,
            strategy_name="intelligence_synthesis",
            timestamp=T0,
        )

    stub = StubIntelligence(signal_factory=_signal)
    loop, _fake, _adapter, _recorder, _store = _rig(
        entries_enabled=True, intelligence=stub
    )

    await loop.run_once()

    assert "produce_thesis" in stub.calls and "thesis_to_signal" in stub.calls
    assert loop.stats.signals >= 1, "the thesis signal reached the normal signal path"
    assert loop.position_management["entries_enabled"] is True
