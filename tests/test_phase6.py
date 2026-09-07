"""Phase 6 tests — order manager lifecycle + mock execution adapter."""

from __future__ import annotations

from typing import Any

import pytest

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, OrderSide, OrderStatus
from mt5_platform.common.events import (
    AccountSnapshot,
    OrderRequest,
    RiskDecision,
    StrategySignal,
)
from mt5_platform.config import Settings
from mt5_platform.execution import (
    MockExecutionAdapter,
    build_execution_adapter,
)
from mt5_platform.orders import (
    ACTIVE_STATUSES,
    OrderManager,
    OrderSubmissionBlocked,
)
from mt5_platform.risk import RiskContext, RiskEngine
from mt5_platform.storage import InMemoryMarketDataStore


def make_signal(
    *,
    symbol: str = "XAUUSD",
    side: OrderSide = OrderSide.BUY,
    entry: float = 2500.0,
    stop_loss: float | None = 2490.0,
) -> StrategySignal:
    return StrategySignal(
        symbol=symbol,
        direction=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=entry + 20.0,
        confidence=0.8,
        reason="phase6_test",
    )


def make_account(**overrides: Any) -> AccountSnapshot:
    base: dict[str, Any] = {
        "balance": 10_000.0,
        "equity": 10_000.0,
        "free_margin": 10_000.0,
        "used_margin": 0.0,
        "floating_pnl": 0.0,
    }
    base.update(overrides)
    return AccountSnapshot(**base)


async def connect_mock(**kwargs: Any) -> MockExecutionAdapter:
    adapter = MockExecutionAdapter(**kwargs)
    await adapter.connect()
    return adapter


def recent_events(limit: int = 100) -> list[Any]:
    return audit_log.recent(limit=limit)


# ---------------------------------------------------------------------------
# Order state machine
# ---------------------------------------------------------------------------


def test_full_happy_path_lifecycle() -> None:
    mgr = OrderManager()
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    assert order.status is OrderStatus.CREATED

    mgr.apply_risk_decision(order.order_id, RiskDecision(approved=True))
    assert mgr.get(order.order_id).status is OrderStatus.APPROVED

    mgr.transition(order.order_id, OrderStatus.SUBMITTED)
    mgr.transition(order.order_id, OrderStatus.ACCEPTED)
    final = mgr.transition(order.order_id, OrderStatus.FILLED)
    assert final.status is OrderStatus.FILLED

    history = mgr.history(order.order_id)
    assert [h["to"] for h in history] == [
        "risk_check",
        "approved",
        "submitted",
        "accepted",
        "filled",
    ]
    assert all(not h["forced"] for h in history)


def test_invalid_transition_rejected_and_audited() -> None:
    from mt5_platform.orders import InvalidTransitionError

    mgr = OrderManager()
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    with pytest.raises(InvalidTransitionError):
        mgr.transition(order.order_id, OrderStatus.FILLED)
    # Invalid attempt must not mutate state.
    assert mgr.get(order.order_id).status is OrderStatus.CREATED
    assert mgr.stats_snapshot()["stats"]["invalid_transitions"] == 1
    events = recent_events()
    assert any(e.event_type == AuditEventType.ORDER_TRANSITION_REJECTED for e in events)


def test_reconciliation_only_force_transition_allowed() -> None:
    mgr = OrderManager()
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    forced = mgr.force_transition(
        order.order_id, OrderStatus.BROKER_REJECTED, reason="broker_reconciliation"
    )
    assert forced.status is OrderStatus.BROKER_REJECTED
    history = mgr.history(order.order_id)
    assert history[-1]["forced"] is True
    assert history[-1]["reason"] == "broker_reconciliation"


def test_risk_rejection_marks_order_rejected() -> None:
    mgr = OrderManager()
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    decision = RiskDecision(approved=False, reasons=["max_risk_per_trade"])
    mgr.apply_risk_decision(order.order_id, decision)
    stored = mgr.get(order.order_id)
    assert stored is not None
    assert stored.status is OrderStatus.REJECTED
    events = recent_events()
    rejected = [e for e in events if e.event_type == AuditEventType.ORDER_REJECTED]
    assert rejected
    with_reasons = next(e for e in rejected if "reasons" in e.payload)
    assert with_reasons.payload["reasons"] == ["max_risk_per_trade"]


def test_recent_orders_and_status_filters() -> None:
    mgr = OrderManager()
    for _ in range(3):
        order = mgr.create_from_signal(make_signal(), volume=0.01)
        mgr.apply_risk_decision(order.order_id, RiskDecision(approved=False))
    assert len(mgr.recent_orders(2)) == 2
    rejected = mgr.orders_by_status(OrderStatus.REJECTED)
    assert len(rejected) == 3
    assert mgr.stats_snapshot()["stats"]["risk_rejected"] == 3


# ---------------------------------------------------------------------------
# Mock execution adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_fill_lifecycle_with_persistence() -> None:
    store = InMemoryMarketDataStore()
    adapter = await connect_mock(slippage=0.05)
    mgr = OrderManager(
        settings=Settings(), store=store, risk_engine=RiskEngine(settings=Settings())
    )

    order, decision, record = await mgr.process_signal(
        make_signal(),
        RiskContext(account=make_account(), proposed_volume=0.02),
        adapter,
    )
    assert decision.approved
    assert record is not None
    assert record.final_status is OrderStatus.FILLED
    assert record.filled_volume == 0.02
    assert record.execution_price == pytest.approx(2500.05)
    assert record.slippage == pytest.approx(0.05)

    stored = mgr.get(order.order_id)
    assert stored is not None
    assert stored.status is OrderStatus.FILLED
    assert stored.filled_volume == 0.02
    # Filled orders stay active until closed; rejections are terminal.
    assert stored.status in ACTIVE_STATUSES
    # Store sink persisted order + execution.
    assert any(o.order_id == order.order_id for o in store.orders)
    assert any(e.order_id == order.order_id for e in store.executions)

    events = recent_events()
    types = {e.event_type for e in events}
    assert {
        AuditEventType.ORDER_CREATED,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.ORDER_FILLED,
    } <= types


@pytest.mark.asyncio
async def test_mock_partial_fill_sets_filled_volume() -> None:
    adapter = await connect_mock(fill_ratio=0.5)
    mgr = OrderManager(settings=Settings(), risk_engine=RiskEngine(settings=Settings()))
    order, _decision, record = await mgr.process_signal(
        make_signal(),
        RiskContext(account=make_account(), proposed_volume=0.10),
        adapter,
    )
    assert record is not None
    assert record.final_status is OrderStatus.PARTIALLY_FILLED
    assert record.filled_volume == pytest.approx(0.05)
    stored = mgr.get(order.order_id)
    assert stored is not None
    assert stored.status is OrderStatus.PARTIALLY_FILLED
    assert stored.filled_volume == pytest.approx(0.05)
    # Partially filled order is still active and opened a partial position.
    assert stored.status in ACTIVE_STATUSES
    assert len(adapter.positions) == 1
    assert next(iter(adapter.positions.values())).volume == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_mock_broker_rejection() -> None:
    adapter = await connect_mock(fill=False)
    mgr = OrderManager(settings=Settings(), risk_engine=RiskEngine(settings=Settings()))
    order, _decision, record = await mgr.process_signal(
        make_signal(),
        RiskContext(account=make_account(), proposed_volume=0.01),
        adapter,
    )
    assert record is not None
    assert record.final_status is OrderStatus.BROKER_REJECTED
    assert record.rejection_reason == "mock_rejection"
    assert record.execution_price is None
    stored = mgr.get(order.order_id)
    assert stored is not None
    assert stored.status is OrderStatus.BROKER_REJECTED
    assert stored.rejection_reason == "mock_rejection"
    assert not adapter.positions  # rejected orders never open positions


@pytest.mark.asyncio
async def test_mock_sell_slippage_direction() -> None:
    adapter = await connect_mock(slippage=0.10)
    await adapter.submit_order(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.SELL,
            volume=0.01,
            entry=2500.0,
            status=OrderStatus.APPROVED,
        )
    )
    sell_fill = adapter.executions[-1]
    # SELL fills lower: slippage moves price against the trader.
    assert sell_fill.execution_price == pytest.approx(2499.90)


@pytest.mark.asyncio
async def test_mock_scripted_outcomes() -> None:
    adapter = await connect_mock(outcomes=[OrderStatus.BROKER_REJECTED, OrderStatus.FILLED])
    approved = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry=2500.0,
        status=OrderStatus.APPROVED,
    )
    first = await adapter.submit_order(approved)
    second = await adapter.submit_order(approved)
    assert first.final_status is OrderStatus.BROKER_REJECTED
    assert second.final_status is OrderStatus.FILLED


@pytest.mark.asyncio
async def test_mock_account_floating_pnl_margin_and_close() -> None:
    adapter = await connect_mock()
    await adapter.submit_order(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            volume=1.0,
            entry=2500.0,
            status=OrderStatus.APPROVED,
        )
    )
    adapter.set_market_price("XAUUSD", 2510.0)

    snapshot = await adapter.get_account()
    assert snapshot.open_positions == 1
    assert snapshot.floating_pnl == pytest.approx(10.0)  # point_value=1
    assert snapshot.equity == pytest.approx(10_010.0)
    # Used margin = 1.0 * 2500 * 1% = 25
    assert snapshot.used_margin == pytest.approx(25.0)
    assert snapshot.free_margin == pytest.approx(10_010.0 - 25.0)
    assert snapshot.margin_level == pytest.approx(10_010.0 / 25.0 * 100.0)

    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].current_price == pytest.approx(2510.0)
    assert positions[0].floating_pnl == pytest.approx(10.0)

    close = await adapter.close_position(positions[0].ticket)
    assert close.final_status is OrderStatus.CLOSED
    assert close.mt5_response["realized_pnl"] == pytest.approx(10.0)
    assert not adapter.positions
    after = await adapter.get_account()
    assert after.balance == pytest.approx(10_010.0)  # realized into balance
    assert after.floating_pnl == 0.0


@pytest.mark.asyncio
async def test_mock_close_unknown_position_raises() -> None:
    adapter = await connect_mock()
    with pytest.raises(KeyError):
        await adapter.close_position("no_such_ticket")


@pytest.mark.asyncio
async def test_unconnected_adapter_rejects_orders() -> None:
    adapter = MockExecutionAdapter()
    order = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry=2500.0,
        status=OrderStatus.APPROVED,
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.submit_order(order)


@pytest.mark.asyncio
async def test_adapter_refuses_unapproved_order() -> None:
    adapter = await connect_mock()
    created = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry=2500.0,
        status=OrderStatus.CREATED,
    )
    with pytest.raises(ValueError, match="risk-approved"):
        await adapter.submit_order(created)


# ---------------------------------------------------------------------------
# OrderManager submission pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_only_approved_orders() -> None:
    adapter = await connect_mock()
    mgr = OrderManager(settings=Settings())
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    with pytest.raises(ValueError, match="APPROVED"):
        await mgr.submit_order(order.order_id, adapter)


@pytest.mark.asyncio
async def test_kill_switch_blocks_submission() -> None:
    settings = Settings(emergency_kill_switch=True)
    adapter = await connect_mock()
    mgr = OrderManager(settings=settings)
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    mgr.apply_risk_decision(order.order_id, RiskDecision(approved=True))
    with pytest.raises(OrderSubmissionBlocked, match="kill switch"):
        await mgr.submit_order(order.order_id, adapter)
    # Order stays APPROVED — nothing was sent to the broker.
    assert mgr.get(order.order_id).status is OrderStatus.APPROVED
    assert all(
        e.payload.get("order_id") != order.order_id
        for e in recent_events()
        if e.event_type == AuditEventType.ORDER_SUBMITTED
    )


@pytest.mark.asyncio
async def test_risk_engine_kill_switch_blocks_submission() -> None:
    adapter = await connect_mock()
    engine = RiskEngine(settings=Settings())
    engine.engage_kill_switch("test_halt")
    mgr = OrderManager(settings=Settings(), risk_engine=engine)
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    mgr.apply_risk_decision(order.order_id, RiskDecision(approved=True))
    with pytest.raises(OrderSubmissionBlocked):
        await mgr.submit_order(order.order_id, adapter)


@pytest.mark.asyncio
async def test_risk_rejected_signal_never_reaches_adapter() -> None:
    adapter = await connect_mock()
    settings = Settings(max_risk_per_trade_pct=0.0)  # impossible → always rejected
    engine = RiskEngine(settings=settings)
    mgr = OrderManager(settings=settings, risk_engine=engine)
    order, decision, record = await mgr.process_signal(
        make_signal(),
        RiskContext(account=make_account(), proposed_volume=0.01),
        adapter,
    )
    assert not decision.approved
    assert record is None
    assert order.status is OrderStatus.REJECTED
    assert not adapter.executions  # adapter never saw the order


@pytest.mark.asyncio
async def test_missing_risk_engine_fails_closed() -> None:
    adapter = await connect_mock()
    mgr = OrderManager(settings=Settings())  # no risk engine
    order, decision, record = await mgr.process_signal(
        make_signal(),
        RiskContext(account=make_account(), proposed_volume=0.01),
        adapter,
    )
    assert not decision.approved
    assert "no_risk_engine_configured" in decision.reasons
    assert record is None
    assert order.status is OrderStatus.REJECTED
    assert not adapter.executions


@pytest.mark.asyncio
async def test_process_signal_requires_positive_volume() -> None:
    adapter = await connect_mock()
    mgr = OrderManager(settings=Settings())
    with pytest.raises(ValueError, match="proposed_volume"):
        await mgr.process_signal(
            make_signal(),
            RiskContext(account=make_account(), proposed_volume=0.0),
            adapter,
        )


@pytest.mark.asyncio
async def test_adapter_exception_marks_order_failed() -> None:
    class ExplodingAdapter(MockExecutionAdapter):
        async def submit_order(self, order: OrderRequest) -> Any:
            raise RuntimeError("broker connection lost")

    adapter = ExplodingAdapter()
    await adapter.connect()
    mgr = OrderManager(settings=Settings())
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    mgr.apply_risk_decision(order.order_id, RiskDecision(approved=True))
    with pytest.raises(RuntimeError, match="connection lost"):
        await mgr.submit_order(order.order_id, adapter)
    stored = mgr.get(order.order_id)
    assert stored is not None
    assert stored.status is OrderStatus.FAILED
    events = recent_events()
    assert any(
        e.event_type == AuditEventType.EXECUTION_ERROR
        and e.error is not None
        and e.payload.get("order_id") == order.order_id
        for e in events
    )


@pytest.mark.asyncio
async def test_store_sink_can_be_disabled() -> None:
    store = InMemoryMarketDataStore()
    settings = Settings(order_store_sink_enabled=False)
    adapter = await connect_mock()
    mgr = OrderManager(settings=settings, store=store)
    await mgr.process_signal(
        make_signal(),
        RiskContext(account=make_account(), proposed_volume=0.01),
        adapter,
    )
    assert not store.orders
    assert not store.executions


# ---------------------------------------------------------------------------
# Reconciliation — broker state is truth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_reports_mismatch_and_corrects() -> None:
    adapter = await connect_mock()
    mgr = OrderManager(settings=Settings())
    # Simulate a crash-recovery scenario: local thinks SUBMITTED, broker says filled.
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    mgr.apply_risk_decision(order.order_id, RiskDecision(approved=True))
    mgr.transition(order.order_id, OrderStatus.SUBMITTED)
    adapter._broker_orders[order.order_id] = OrderStatus.FILLED

    report = await mgr.reconcile(adapter)
    assert report["checked"] == 1
    assert report["mismatches"] == [
        {
            "order_id": order.order_id,
            "local": "submitted",
            "broker": "filled",
        }
    ]
    # Local state was corrected to broker truth.
    assert mgr.get(order.order_id).status is OrderStatus.FILLED
    assert mgr.stats_snapshot()["stats"]["reconciliation_mismatches"] == 1
    events = recent_events()
    assert any(e.event_type == AuditEventType.RECONCILIATION_MISMATCH for e in events)


@pytest.mark.asyncio
async def test_reconciliation_clean_reports_completed() -> None:
    adapter = await connect_mock()
    mgr = OrderManager(settings=Settings())
    order = mgr.create_from_signal(make_signal(), volume=0.01)
    mgr.apply_risk_decision(order.order_id, RiskDecision(approved=True))
    mgr.transition(order.order_id, OrderStatus.SUBMITTED)
    adapter._broker_orders[order.order_id] = OrderStatus.SUBMITTED

    report = await mgr.reconcile(adapter)
    assert report["checked"] == 1
    assert report["mismatches"] == []
    assert mgr.get(order.order_id).status is OrderStatus.SUBMITTED
    events = recent_events()
    assert any(e.event_type == AuditEventType.RECONCILIATION_COMPLETED for e in events)


@pytest.mark.asyncio
async def test_broker_order_states_filtering() -> None:
    adapter = await connect_mock()
    a = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry=2500.0,
        status=OrderStatus.APPROVED,
    )
    b = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.01,
        entry=2500.0,
        status=OrderStatus.APPROVED,
    )
    await adapter.submit_order(a)
    await adapter.submit_order(b)
    states = await adapter.broker_order_states([a.order_id])
    assert set(states) == {a.order_id}


# ---------------------------------------------------------------------------
# Factory + config
# ---------------------------------------------------------------------------


def test_factory_builds_mock_from_settings() -> None:
    adapter = build_execution_adapter(Settings())
    assert isinstance(adapter, MockExecutionAdapter)


def test_factory_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown execution backend"):
        build_execution_adapter(Settings(execution_backend="binance"))


@pytest.mark.asyncio
async def test_mt5_backend_is_phase7_stub() -> None:
    from mt5_platform.execution import MT5ExecutionAdapter

    adapter = MT5ExecutionAdapter()
    assert await adapter.is_connected() is False
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await adapter.connect()
    with pytest.raises(NotImplementedError, match="Phase 7"):
        await adapter.submit_order(
            OrderRequest(symbol="XAUUSD", side=OrderSide.BUY, volume=0.01, entry=2500.0)
        )


def test_invalid_fill_ratio_rejected() -> None:
    with pytest.raises(ValueError, match="fill_ratio"):
        MockExecutionAdapter(fill_ratio=0.0)
    with pytest.raises(ValueError, match="fill_ratio"):
        MockExecutionAdapter(fill_ratio=1.5)


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_api_orders_and_executions_endpoints() -> None:
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from mt5_platform.api import create_app
    from mt5_platform.config import Settings

    app = create_app(Settings(storage_backend="memory", execution_backend="mock"))

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = (await client.get("/api/v1/status")).json()
            assert status["phase"] == 6
            assert status["execution_backend"] == "mock"

            health = (await client.get("/health")).json()
            assert health["components"]["execution"] == "up"

            assert (await client.get("/api/v1/orders")).json()["orders"] == []
            assert (await client.get("/api/v1/executions")).json()["executions"] == []
            assert (await client.get("/api/v1/orders")).json()["stats"]["total"] == 0

            missing = await client.get("/api/v1/orders/does_not_exist")
            assert missing.status_code == 404

    asyncio.run(run())
