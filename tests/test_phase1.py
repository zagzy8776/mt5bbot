"""Phase 1 architecture tests — config gates, schemas, risk, orders, mock execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from mt5_platform.common.enums import OrderSide, OrderStatus, ProxyErrorType, ProxyState
from mt5_platform.common.events import AccountSnapshot, MarketDataEvent, StrategySignal
from mt5_platform.config import Settings, TradingMode, clear_settings_cache
from mt5_platform.execution import MockExecutionAdapter
from mt5_platform.ingestion.proxy_manager import ProxyManager
from mt5_platform.ingestion.resource_policy import should_abort
from mt5_platform.orders import InvalidTransitionError, OrderManager
from mt5_platform.pipeline import MarketDataValidator
from mt5_platform.risk import RiskContext, RiskEngine


def test_default_mode_is_demo() -> None:
    clear_settings_cache()
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        live_trading_enabled=False,
        live_trading_acknowledged=False,
    )
    assert settings.is_demo
    assert not settings.is_live


def test_live_mode_requires_dual_ack() -> None:
    with pytest.raises(ValidationError):
        Settings(
            trading_mode=TradingMode.LIVE,
            live_trading_enabled=True,
            live_trading_acknowledged=False,
        )
    with pytest.raises(ValidationError):
        Settings(
            trading_mode=TradingMode.LIVE,
            live_trading_enabled=False,
            live_trading_acknowledged=True,
        )


def test_live_mode_allowed_when_explicitly_gated() -> None:
    settings = Settings(
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        live_trading_acknowledged=True,
    )
    assert settings.is_live


def test_market_data_event_normalizes_symbol_and_timestamp() -> None:
    naive = datetime(2026, 9, 6, 12, 0, 0)
    event = MarketDataEvent(
        timestamp=naive,
        source="test",
        symbol="xauusd",
        bid=2500.0,
        ask=2500.5,
        price=2500.25,
    )
    assert event.symbol == "XAUUSD"
    assert event.timestamp.tzinfo is not None


def test_validator_rejects_stale_and_duplicate() -> None:
    now = datetime.now(UTC)
    validator = MarketDataValidator(stale_max_age_ms=1000)
    event = MarketDataEvent(
        timestamp=now - timedelta(seconds=5),
        source="test",
        symbol="XAUUSD",
        price=2500.0,
    )
    result = validator.validate(event, now=now)
    assert not result.accepted
    assert "stale_data" in result.reasons

    fresh = MarketDataEvent(timestamp=now, source="test", symbol="XAUUSD", price=2500.0)
    assert validator.validate(fresh, now=now).accepted
    dup = validator.validate(fresh, now=now)
    assert not dup.accepted
    assert "duplicate_event" in dup.reasons


def test_risk_engine_blocks_without_stop_loss_and_kill_switch() -> None:
    settings = Settings(trading_mode=TradingMode.DEMO)
    engine = RiskEngine(settings=settings)
    signal = StrategySignal(symbol="XAUUSD", direction=OrderSide.BUY, entry=2500.0, confidence=0.5)
    account = AccountSnapshot(
        balance=10_000,
        equity=10_000,
        free_margin=10_000,
        used_margin=0,
        floating_pnl=0,
    )
    ctx = RiskContext(account=account, stop_loss_required=True)
    decision = engine.evaluate(signal, ctx)
    assert not decision.approved
    assert "stop_loss_required" in decision.reasons

    engine.engage_kill_switch("test")
    signal2 = StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2500.0,
        stop_loss=2490.0,
        confidence=0.5,
    )
    decision2 = engine.evaluate(signal2, ctx)
    assert not decision2.approved
    assert "emergency_kill_switch" in decision2.reasons


def test_order_state_machine_transitions() -> None:
    mgr = OrderManager()
    signal = StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2500.0,
        stop_loss=2490.0,
        take_profit=2520.0,
        confidence=0.7,
    )
    order = mgr.create_from_signal(signal, volume=0.01)
    assert order.status is OrderStatus.CREATED

    from mt5_platform.common.events import RiskDecision

    approved = RiskDecision(approved=True)
    mgr.apply_risk_decision(order.order_id, approved)
    assert mgr.get(order.order_id).status is OrderStatus.APPROVED

    with pytest.raises(InvalidTransitionError):
        mgr.transition(order.order_id, OrderStatus.FILLED)


@pytest.mark.asyncio
async def test_mock_execution_fills_approved_order() -> None:
    adapter = MockExecutionAdapter(fill=True)
    await adapter.connect()
    mgr = OrderManager()
    signal = StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2500.0,
        stop_loss=2490.0,
        confidence=0.8,
    )
    order = mgr.create_from_signal(signal, volume=0.01)
    from mt5_platform.common.events import RiskDecision

    mgr.apply_risk_decision(order.order_id, RiskDecision(approved=True))
    order = mgr.transition(order.order_id, OrderStatus.SUBMITTED)
    record = await adapter.submit_order(order)
    assert record.final_status is OrderStatus.FILLED
    assert record.execution_id.startswith("exec_")


def test_proxy_circuit_breaker_quarantines() -> None:
    pm = ProxyManager()
    pm.register("p1", "http://proxy.example:8080")
    acquired = pm.acquire()
    assert acquired is not None
    assert acquired.state is ProxyState.IN_USE

    pm.release("p1", success=False, error=ProxyErrorType.AUTH)
    assert pm.get("p1").state is ProxyState.QUARANTINED

    pm.register("p2", "http://proxy2.example:8080")
    a2 = pm.acquire()
    assert a2 is not None
    for _ in range(3):
        pm.release("p2", success=False, error=ProxyErrorType.TIMEOUT)
        if pm.get("p2").state is not ProxyState.QUARANTINED:
            assert pm.acquire() is not None or pm.get("p2").state in {
                ProxyState.COOLING,
                ProxyState.QUARANTINED,
                ProxyState.IN_USE,
            }
            if pm.get("p2").state in {ProxyState.COOLING, ProxyState.HEALTHY}:
                pm.acquire()
    assert pm.get("p2").state is ProxyState.QUARANTINED


def test_resource_policy_does_not_abort_until_calibrated() -> None:
    assert should_abort("image", "https://cdn.example/a.png", calibrated=False) is False
    assert should_abort("image", "https://cdn.example/a.png", calibrated=True) is True
    assert should_abort("xhr", "https://api.example/data", calibrated=True) is False
