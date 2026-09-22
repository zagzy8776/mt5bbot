"""External/manual MT5 positions: discovery, policy routing, and lifecycle audit.

MANUAL_POSITION_POLICY decides what the bot may do with a broker position it did not open:
`ignore` = visible and audited only, `observe` = evaluated but never executed, and `manage` =
executed like a bot-owned position. Every action still passes RiskEngine -> OrderManager.
"""

from __future__ import annotations

import inspect
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from mt5_platform.account import AccountMonitor
from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import (
    AuditEventType,
    PositionDecision,
    ThesisStatus,
)
from mt5_platform.common.events import AuditEvent
from mt5_platform.config import ManualPositionPolicy, Settings
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter
from mt5_platform.orders import OrderManager
from mt5_platform.position.models import PositionDecisionModel
from mt5_platform.risk import RiskEngine
from mt5_platform.runtime import IntelligenceLayer, Quote, TradingLoop
from mt5_platform.signals import SignalEngine
from tests.fake_mt5 import FakeMT5
from tests.test_runtime import AlwaysBuy, FakeFeed, _bars

MANUAL_TICKET = 3264036722  # the real demo manual trade: XAUUSD BUY 0.04 @ 4317.478, magic 0
MANUAL_ENTRY = 4317.478
MANUAL_PRICE = 4332.0
MANUAL_VOLUME = 0.04
T0 = datetime(2024, 1, 2, 9, tzinfo=UTC)


def _broker_position(
    *,
    ticket: int = MANUAL_TICKET,
    magic: int = 0,
    volume: float = MANUAL_VOLUME,
    price_open: float = MANUAL_ENTRY,
    price_current: float = MANUAL_PRICE,
    side: int = FakeMT5.POSITION_TYPE_BUY,
    sl: float = 0.0,
    tp: float = 0.0,
    comment: str = "",
) -> SimpleNamespace:
    """A broker position exactly as MT5 returns it (magic 0 = opened outside this bot)."""
    return SimpleNamespace(
        ticket=ticket,
        symbol="XAUUSD",
        volume=volume,
        type=side,
        price_open=price_open,
        price_current=price_current,
        profit=57.88,
        sl=sl,
        tp=tp,
        magic=magic,
        comment=comment,
        time=int(time.time()),
    )


def _decision(
    action: PositionDecision,
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    volume: float | None = None,
    reason: str = "test decision",
) -> PositionDecisionModel:
    """A PositionManager decision, in the real model type the manager produces."""
    return PositionDecisionModel(
        position_id=str(MANUAL_TICKET),
        original_thesis_id="thesis_test",
        current_context_id="ctx_test",
        thesis_status=ThesisStatus.UNKNOWN,
        decision=action,
        confidence=0.9,
        proposed_stop_loss=stop_loss,
        proposed_take_profit=take_profit,
        proposed_volume=volume,
        reason=reason,
    )


class StubIntelligence:
    """Deterministic stand-in for IntelligenceLayer: one scripted decision per position."""

    def __init__(self, *decisions: PositionDecisionModel) -> None:
        self._decisions = list(decisions)
        self.evaluated: list[str] = []
        self.stats = SimpleNamespace(position_exits=0, errors=0)

    def feed_tick(self, *, bid: float, ask: float) -> Any:
        return SimpleNamespace(usable_for_trading=True)

    def produce_thesis(self, ctx: Any) -> None:
        return None

    def thesis_to_signal(self, thesis: Any) -> None:
        return None

    def evaluate_positions(
        self, positions: list[Any], ctx: Any
    ) -> list[tuple[Any, PositionDecisionModel]]:
        self.evaluated.extend(p.ticket for p in positions)
        if not self._decisions:
            return []
        return [(p, self._decisions[i % len(self._decisions)]) for i, p in enumerate(positions)]


class SpyAdapter(MT5ExecutionAdapter):
    """Real adapter behavior plus a record of every position modification/closure request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.modify_calls: list[tuple[str, float | None, float | None]] = []
        self.close_calls: list[tuple[str, float | None]] = []

    async def modify_position(
        self, ticket: str, *, stop_loss: float | None, take_profit: float | None
    ) -> Any:
        self.modify_calls.append((ticket, stop_loss, take_profit))
        return await super().modify_position(ticket, stop_loss=stop_loss, take_profit=take_profit)

    async def close_position(self, ticket: str, *, volume: float | None = None) -> Any:
        self.close_calls.append((ticket, volume))
        return await super().close_position(ticket, volume=volume)


def _settings(policy: ManualPositionPolicy, **kw: Any) -> Settings:
    # risk_state_path="" keeps a developer's persisted kill switch out of the test run.
    return Settings(
        execution_backend="mt5",
        manual_position_policy=policy,
        risk_state_path="",
        **kw,
    )


async def _rig(
    policy: ManualPositionPolicy,
    *decisions: PositionDecisionModel,
    positions: list[SimpleNamespace] | None = None,
    settings_kw: dict[str, Any] | None = None,
) -> tuple[TradingLoop, FakeMT5, SpyAdapter, StubIntelligence, FakeFeed, RiskEngine]:
    fake = FakeMT5(balance=10_000.0)
    fake.positions = list(positions if positions is not None else [_broker_position()])
    settings = _settings(policy, **(settings_kw or {}))
    adapter = SpyAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    stub = StubIntelligence(*decisions)
    feed = FakeFeed(_bars(), Quote(MANUAL_PRICE, MANUAL_PRICE + 0.3, 30.0, 0.0))
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,
        feed=feed,
        signal_engine=SignalEngine([]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=["XAUUSD"],
        poll_s=0.01,
        warmup_bars=3,
        reconcile_every_s=0.0,
        intelligence=stub,
    )
    return loop, fake, adapter, stub, feed, risk


def _events(ticket: int | None = None, component: str = "position_lifecycle") -> list[AuditEvent]:
    events = [e for e in audit_log.recent(limit=500) if e.component == component]
    if ticket is not None:
        events = [e for e in events if e.correlation_id == str(ticket)]
    return events


def _types(ticket: int | None = None) -> list[str]:
    return [e.event_type for e in _events(ticket)]


def _of(event_type: AuditEventType, ticket: int = MANUAL_TICKET) -> list[AuditEvent]:
    return [e for e in _events(ticket) if e.event_type == event_type.value]


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    audit_log.clear()
    yield
    audit_log.clear()


# ------------------------------------------------------------------ broker truth


async def test_adapter_reports_manual_and_bot_positions_with_external_flag() -> None:
    """The adapter must return every broker position, tagging the manual one as external."""
    settings = _settings(ManualPositionPolicy.IGNORE)
    fake = FakeMT5()
    fake.positions = [
        _broker_position(),
        _broker_position(ticket=9001, magic=settings.mt5_magic, volume=0.02, comment="tag_abc"),
    ]
    adapter = MT5ExecutionAdapter(settings, client=fake)
    await adapter.connect()

    positions = {p.ticket: p for p in await adapter.get_positions()}
    assert set(positions) == {str(MANUAL_TICKET), "9001"}

    manual = positions[str(MANUAL_TICKET)]
    assert manual.is_external is True and manual.magic == 0 and manual.comment == ""

    bot = positions["9001"]
    assert bot.is_external is False and bot.magic == settings.mt5_magic


async def test_ignore_policy_audits_discovery_but_never_evaluates_or_executes() -> None:
    loop, fake, adapter, stub, _, _ = await _rig(
        ManualPositionPolicy.IGNORE, _decision(PositionDecision.EXIT)
    )
    await loop.run_once()
    await loop.run_once()  # same broker view: discovery/reconcile must not repeat

    types = _types(MANUAL_TICKET)
    assert types.count(AuditEventType.POSITION_DISCOVERED.value) == 1
    assert types.count(AuditEventType.POSITION_RECONCILED.value) == 1

    hold = _of(AuditEventType.POSITION_HOLD)[-1]
    assert hold.payload["managed"] is False
    assert hold.payload["policy"] == "ignore"
    assert hold.payload["external"] is True

    assert stub.evaluated == []  # never evaluated under ignore
    assert adapter.modify_calls == [] and adapter.close_calls == []
    assert fake.sent == [] and len(fake.positions) == 1


async def test_broker_state_change_re_emits_the_reconciled_event() -> None:
    loop, fake, _, _, _, _ = await _rig(ManualPositionPolicy.IGNORE)
    await loop.run_once()
    fake.positions[0].sl = 4310.0  # broker-side change (e.g. the trader added a stop)
    await loop.run_once()

    assert _types(MANUAL_TICKET).count(AuditEventType.POSITION_RECONCILED.value) == 2
    assert _types(MANUAL_TICKET).count(AuditEventType.POSITION_DISCOVERED.value) == 1


# ----------------------------------------------------------------------- observe


async def test_observe_policy_monitors_and_refuses_execution() -> None:
    loop, fake, adapter, stub, _, _ = await _rig(
        ManualPositionPolicy.OBSERVE,
        _decision(PositionDecision.EXIT, reason="thesis invalidated"),
    )
    await loop.run_once()

    types = _types(MANUAL_TICKET)
    assert types.index(AuditEventType.POSITION_MONITORED.value) < types.index(
        AuditEventType.POSITION_EXIT_REJECTED.value
    )
    # observe never turns a decision into an execution request
    assert AuditEventType.POSITION_EXIT_REQUESTED.value not in types

    rejected = _of(AuditEventType.POSITION_EXIT_REJECTED)[-1]
    assert rejected.payload["blocked_by"] == "manual_position_policy=observe"
    assert rejected.payload["decision"] == "exit"
    assert rejected.payload["reason"] == "thesis invalidated"
    assert rejected.severity.value == "info"

    assert stub.evaluated == [str(MANUAL_TICKET)]  # evaluated, but never executed
    assert adapter.modify_calls == [] and adapter.close_calls == []
    assert fake.sent == [] and len(fake.positions) == 1


# ------------------------------------------------------------------------ manage


async def test_manage_policy_executes_an_exit_through_risk_and_orders() -> None:
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE,
        _decision(PositionDecision.EXIT, reason="thesis invalidated"),
    )
    await loop.run_once()

    assert adapter.close_calls == [(str(MANUAL_TICKET), None)]
    assert fake.positions == []  # broker truth: the manual position is closed

    types = _types(MANUAL_TICKET)
    assert types.index(AuditEventType.POSITION_EXIT_REQUESTED.value) < types.index(
        AuditEventType.POSITION_CLOSED.value
    )

    closed = _of(AuditEventType.POSITION_CLOSED)[-1]
    assert closed.payload["risk_approved"] is True
    assert closed.payload["execution"]["final_status"] == "closed"
    assert closed.payload["execution"]["mt5_response"]["action"] == "close"

    risk_events = [
        e
        for e in _events(component="risk")
        if e.payload.get("action") == "exit" and e.payload.get("ticket") == str(MANUAL_TICKET)
    ]
    assert risk_events and risk_events[-1].event_type == AuditEventType.RISK_APPROVED.value


async def test_manage_policy_hold_touches_nothing() -> None:
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE, _decision(PositionDecision.HOLD, reason="thesis valid")
    )
    await loop.run_once()

    hold = _of(AuditEventType.POSITION_HOLD)[-1]
    assert hold.payload["policy"] == "manage"
    assert hold.payload["managed"] is True
    assert AuditEventType.POSITION_EXIT_REQUESTED.value not in _types(MANUAL_TICKET)
    assert adapter.modify_calls == [] and adapter.close_calls == []
    assert len(fake.positions) == 1


# ----------------------------------------------------------- protected modifications


async def test_manage_policy_may_add_a_first_protective_stop_to_a_manual_position() -> None:
    """Giving an unprotected manual trade its first stop lowers risk, so it is allowed."""
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE, _decision(PositionDecision.MODIFY, stop_loss=4320.0)
    )
    await loop.run_once()

    assert adapter.modify_calls == [(str(MANUAL_TICKET), 4320.0, None)]
    assert fake.positions[0].sl == 4320.0
    modified = _of(AuditEventType.POSITION_MODIFIED)[-1]
    assert modified.payload["decision"] == "modify"
    assert modified.payload["risk_approved"] is True


async def test_risk_refuses_a_stop_move_that_increases_risk() -> None:
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE,
        _decision(PositionDecision.MODIFY, stop_loss=4300.0),  # below the existing 4320 stop
        positions=[_broker_position(sl=4320.0)],
    )
    await loop.run_once()

    assert adapter.modify_calls == []
    assert fake.positions[0].sl == 4320.0  # untouched
    rejected = _of(AuditEventType.POSITION_EXIT_REJECTED)[-1]
    assert rejected.payload["risk_approved"] is False
    assert rejected.payload["risk_reasons"] == ["stop_loss_increases_risk"]
    assert rejected.severity.value == "warning"


async def test_risk_refuses_a_stop_on_the_wrong_side_of_price() -> None:
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE, _decision(PositionDecision.MODIFY, stop_loss=4340.0)
    )
    await loop.run_once()

    assert adapter.modify_calls == []
    reasons = _of(AuditEventType.POSITION_EXIT_REJECTED)[-1].payload["risk_reasons"]
    assert reasons == ["stop_loss_wrong_side"]


async def test_manage_policy_reduces_the_manual_position_partially() -> None:
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE,
        _decision(PositionDecision.REDUCE, volume=0.02, reason="thesis weakening"),
    )
    await loop.run_once()

    assert adapter.close_calls == [(str(MANUAL_TICKET), 0.02)]
    assert [p.volume for p in fake.positions] == [pytest.approx(0.02)]
    reduced = _of(AuditEventType.POSITION_REDUCED)[-1]
    assert reduced.payload["decision"] == "reduce"
    assert reduced.payload["reason"] == "thesis weakening"


async def test_risk_refuses_a_reduce_that_is_not_a_genuine_partial_volume() -> None:
    loop, _, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE, _decision(PositionDecision.REDUCE, volume=MANUAL_VOLUME)
    )
    await loop.run_once()
    assert adapter.close_calls == []
    assert _of(AuditEventType.POSITION_EXIT_REJECTED)[-1].payload["risk_reasons"] == [
        "reduce_volume_not_partial"
    ]

    loop2, _, adapter2, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE, _decision(PositionDecision.REDUCE)
    )
    await loop2.run_once()
    assert adapter2.close_calls == []
    assert _of(AuditEventType.POSITION_EXIT_REJECTED)[-1].payload["risk_reasons"] == [
        "reduce_volume_required"
    ]


# --------------------------------------------------- policy scope and entry limits


async def test_bot_owned_positions_are_managed_even_under_ignore_policy() -> None:
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.IGNORE, _decision(PositionDecision.EXIT), positions=[]
    )
    fake.positions.append(_broker_position(ticket=9001, magic=loop.settings.mt5_magic))
    await loop.run_once()

    assert adapter.close_calls == [("9001", None)]
    assert fake.positions == []


async def test_kill_switch_still_allows_a_risk_reducing_exit() -> None:
    """The kill switch stops NEW risk; it must never trap an open position."""
    loop, fake, adapter, _, _, risk = await _rig(
        ManualPositionPolicy.MANAGE, _decision(PositionDecision.EXIT)
    )
    risk.engage_kill_switch("test_halt")
    await loop.run_once()

    assert risk.kill_switch is True
    assert adapter.close_calls == [(str(MANUAL_TICKET), None)]
    assert fake.positions == []


async def test_external_position_counts_for_duplicate_and_parallel_position_limits() -> None:
    """Manual exposure is real exposure: the bot must not duplicate or ignore it."""
    settings = _settings(ManualPositionPolicy.IGNORE, max_simultaneous_positions=1)
    fake = FakeMT5(balance=10_000.0)
    fake.positions = [_broker_position()]
    adapter = MT5ExecutionAdapter(settings, client=fake)
    risk = RiskEngine(settings=settings)
    loop = TradingLoop(
        settings=settings,
        adapter=adapter,
        feed=FakeFeed(_bars(), Quote(2500.0, 2500.3, 30.0, 0.0)),
        signal_engine=SignalEngine([AlwaysBuy()]),
        risk_engine=risk,
        order_manager=OrderManager(settings=settings, risk_engine=risk),
        monitor=AccountMonitor(settings, risk),
        symbols=["XAUUSD"],
        poll_s=0.01,
        warmup_bars=3,
        reconcile_every_s=0.0,
    )
    await loop.start()
    loop.feed.add_bar()
    await loop.run_once()

    assert fake.sent == []  # no duplicate BUY next to the manual BUY
    assert loop.stats.skipped["risk:duplicate_position"] == 1
    assert loop.stats.skipped["risk:max_simultaneous_positions"] == 1


async def test_lifecycle_events_carry_the_full_manual_position_context() -> None:
    loop, _, _, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE,
        _decision(PositionDecision.EXIT, reason="thesis invalidated"),
    )
    await loop.run_once()

    event = _of(AuditEventType.POSITION_CLOSED)[-1]
    assert event.component == "position_lifecycle"
    assert event.correlation_id == str(MANUAL_TICKET)
    assert event.symbol == "XAUUSD"
    payload = event.payload
    assert payload["ticket"] == str(MANUAL_TICKET)
    assert payload["external"] is True
    assert payload["magic"] == 0
    assert payload["comment"] == ""
    assert payload["policy"] == "manage"
    assert payload["managed"] is True
    assert payload["decision"] == "exit"
    assert payload["reason"] == "thesis invalidated"
    assert payload["thesis_id"] == "thesis_test"
    assert payload["volume"] == pytest.approx(MANUAL_VOLUME)
    assert payload["floating_pnl"] == pytest.approx(57.88)
    assert payload["execution"]["correlation_id"] == str(MANUAL_TICKET)


def test_loop_has_no_direct_broker_call_that_bypasses_risk_and_orders() -> None:
    """Regression guard: the old code closed positions straight through the adapter."""
    source = inspect.getsource(TradingLoop)
    assert "close_position(" not in source
    assert "modify_position(" not in source
    assert "execute_position_decision(" in source


def _feed_history(layer: IntelligenceLayer, n: int = 300) -> None:
    """Feed enough ticks for the context engine to build a real market context."""
    for i in range(n):
        price = 2500.0 + ((i % 20) - 10) * 0.2
        layer.feed_tick(
            bid=price - 0.15, ask=price + 0.15, timestamp=T0 + timedelta(seconds=60 * i)
        )


async def test_real_position_manager_decision_is_routed_and_audited() -> None:
    """The real PositionManager output must reach the lifecycle trail with its reason."""
    loop, fake, adapter, _, _, _ = await _rig(ManualPositionPolicy.MANAGE, positions=[])
    fake.positions.append(_broker_position())
    await adapter.connect()
    layer = IntelligenceLayer(symbol="XAUUSD")
    _feed_history(layer)
    ctx = layer.feed_tick(bid=MANUAL_PRICE - 0.15, ask=MANUAL_PRICE + 0.15)
    assert ctx is not None

    results = layer.evaluate_positions(await adapter.get_positions(), ctx)
    assert len(results) == 1 and layer.stats.errors == 0
    position, decision = results[0]

    await loop._manage_position(position, decision, await adapter.get_account())

    recorded = [e for e in _events(MANUAL_TICKET) if e.payload.get("decision") == decision.decision]
    assert recorded, "the PositionManager decision must appear on the lifecycle trail"
    assert recorded[-1].payload["reason"] == decision.reason
    assert recorded[-1].payload["policy"] == "manage"
    assert recorded[-1].payload["managed"] is True
    if PositionDecision(decision.decision) in (PositionDecision.HOLD, PositionDecision.NO_ACTION):
        assert adapter.modify_calls == [] and adapter.close_calls == []
    else:
        assert adapter.modify_calls or adapter.close_calls


# ------------------------------------------- manage mode may only REDUCE risk (never open)


async def test_manage_policy_may_trail_an_existing_stop_towards_price() -> None:
    """Moving a stop closer to price lowers risk and is allowed."""
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE,
        _decision(PositionDecision.MODIFY, stop_loss=4325.0),
        positions=[_broker_position(sl=4310.0)],
    )
    await loop.run_once()

    assert adapter.modify_calls == [(str(MANUAL_TICKET), 4325.0, None)]
    assert fake.positions[0].sl == 4325.0


async def test_manage_policy_executes_an_emergency_exit_while_entries_stay_blocked() -> None:
    loop, fake, adapter, _, _, risk = await _rig(
        ManualPositionPolicy.MANAGE,
        _decision(PositionDecision.EMERGENCY_EXIT, reason="volatility spike"),
    )
    risk.engage_kill_switch("test_halt")  # new entries blocked, risk reduction still allowed
    await loop.run_once()

    assert risk.kill_switch is True
    assert adapter.close_calls == [(str(MANUAL_TICKET), None)]
    assert fake.positions == []
    assert _of(AuditEventType.POSITION_CLOSED)[-1].payload["decision"] == "emergency_exit"


async def test_manage_mode_cannot_open_a_new_position() -> None:
    """Manage mode is risk-reducing only: it must never submit an entry order."""
    loop, fake, adapter, _, _, _ = await _rig(
        ManualPositionPolicy.MANAGE, _decision(PositionDecision.EXIT)
    )
    opened: list[Any] = []

    async def record_submit(order: Any) -> Any:
        opened.append(order)
        raise AssertionError("manage mode must never submit an order")

    adapter.submit_order = record_submit  # type: ignore[method-assign]
    await loop.run_once()

    assert opened == []  # no entry was even attempted
    assert adapter.close_calls == [(str(MANUAL_TICKET), None)]  # only the reduction
    # Every broker request carried a position id: a close/reduce, never an entry.
    assert len(fake.sent) == 1 and all("position" in req for req in fake.sent)
    assert fake.positions == []


async def test_position_pipeline_refuses_anything_but_modify_reduce_exit() -> None:
    loop, fake, adapter, _, _, _ = await _rig(ManualPositionPolicy.MANAGE)
    await adapter.connect()
    position = (await adapter.get_positions())[0]
    account = await adapter.get_account()

    for action in (PositionDecision.HOLD, PositionDecision.NO_ACTION):
        with pytest.raises(ValueError, match="not executable"):
            await loop.order_manager.execute_position_decision(
                position=position,
                decision=_decision(action),
                account=account,
                adapter=adapter,
            )
    assert adapter.modify_calls == [] and adapter.close_calls == []
    assert len(fake.positions) == 1


def test_position_pipeline_has_no_entry_path() -> None:
    """Structural guard: the position-action path cannot create or submit an order."""
    source = inspect.getsource(OrderManager.execute_position_decision)
    assert "submit_order" not in source
    assert "process_signal" not in source
    assert "create_from_signal" not in source
