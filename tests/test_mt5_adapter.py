"""MT5 adapter tests against a fake terminal: safety rules, idempotency, reconciliation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mt5_platform.common.enums import OrderSide, OrderStatus
from mt5_platform.common.events import OrderRequest, StrategySignal
from mt5_platform.config import Settings, TradingMode
from mt5_platform.execution import build_execution_adapter
from mt5_platform.execution.mt5_adapter import MT5ExecutionAdapter, RealAccountBlocked
from mt5_platform.orders import OrderManager
from mt5_platform.risk import RiskContext, RiskEngine
from tests.fake_mt5 import FakeMT5


def _settings(**kw) -> Settings:
    return Settings(execution_backend="mt5", **kw)


async def _connected(fake: FakeMT5 | None = None, **kw) -> tuple[MT5ExecutionAdapter, FakeMT5]:
    fake = fake or FakeMT5()
    adapter = MT5ExecutionAdapter(_settings(**kw), client=fake)
    await adapter.connect()
    return adapter, fake


def _order(**kw) -> OrderRequest:
    base = dict(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.02,
        entry=2500.3,
        stop_loss=2495.0,
        take_profit=2510.0,
        status=OrderStatus.APPROVED,
    )
    base.update(kw)
    return OrderRequest(**base)


# ------------------------------------------------------------------ connection


async def test_real_account_is_blocked_unless_live_is_acknowledged() -> None:
    fake = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
    adapter = MT5ExecutionAdapter(_settings(), client=fake)
    with pytest.raises(RealAccountBlocked):
        await adapter.connect()
    assert await adapter.is_connected() is False


async def test_real_account_allowed_only_with_both_live_flags() -> None:
    fake = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
    live = Settings(
        execution_backend="mt5",
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        live_trading_acknowledged=True,
    )
    adapter = MT5ExecutionAdapter(live, client=fake)
    await adapter.connect()
    assert await adapter.is_connected() is True


async def test_connect_passes_credentials_to_terminal() -> None:
    fake = FakeMT5()
    adapter = MT5ExecutionAdapter(
        _settings(mt5_login="123456", mt5_password="pw", mt5_server="Exness-MT5Trial"),
        client=fake,
    )
    await adapter.connect()
    assert fake.initialized_with["login"] == 123456
    assert fake.initialized_with["server"] == "Exness-MT5Trial"


def test_factory_builds_mt5_adapter() -> None:
    assert isinstance(build_execution_adapter(_settings()), MT5ExecutionAdapter)


async def test_instrument_spec_comes_from_the_broker() -> None:
    adapter, _ = await _connected()
    spec = await adapter.get_instrument("XAUUSD")
    assert spec is not None
    assert spec.contract_size == 100.0
    assert spec.value_per_price_unit == pytest.approx(100.0)
    assert await adapter.get_instrument("NOPE") is None


# ------------------------------------------------------------------- submitting


async def test_fill_sends_sl_tp_magic_and_tag_with_the_order() -> None:
    adapter, fake = await _connected(mt5_magic=777)
    order = _order()
    record = await adapter.submit_order(order)
    assert record.final_status is OrderStatus.FILLED
    sent = fake.sent[0]
    assert sent["sl"] == 2495.0 and sent["tp"] == 2510.0  # broker holds the protection
    assert sent["magic"] == 777
    assert sent["price"] == 2500.30  # BUY at ask
    assert len(sent["comment"]) <= 31
    assert fake.checked, "order_check must run before order_send"
    positions = await adapter.get_positions()
    assert len(positions) == 1 and positions[0].stop_loss == 2495.0


async def test_sell_uses_bid_and_needs_stop_above() -> None:
    adapter, fake = await _connected()
    ok = await adapter.submit_order(
        _order(side=OrderSide.SELL, stop_loss=2505.0, take_profit=2490.0)
    )
    assert ok.final_status is OrderStatus.FILLED and fake.sent[0]["price"] == 2500.00
    bad = await adapter.submit_order(
        _order(side=OrderSide.SELL, stop_loss=2495.0, take_profit=2490.0)
    )
    assert bad.rejection_reason == "stop_wrong_side"


async def test_order_without_stop_loss_is_never_sent() -> None:
    adapter, fake = await _connected()
    record = await adapter.submit_order(_order(stop_loss=None))
    assert record.final_status is OrderStatus.BROKER_REJECTED
    assert record.rejection_reason == "stop_loss_required"
    assert fake.sent == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"volume": 0.015}, "volume_not_on_step"),
        ({"volume": 0.001}, "volume_below_min"),
        ({"volume": 500.0}, "volume_above_max"),
        ({"stop_loss": 2501.0}, "stop_wrong_side"),
        ({"stop_loss": 2500.25}, "stop_too_close"),
        ({"take_profit": 2499.0}, "take_profit_wrong_side"),
        ({"symbol": "NOPE"}, "unknown_symbol"),
    ],
)
async def test_local_validation_blocks_bad_orders_before_the_broker(overrides, reason) -> None:
    adapter, fake = await _connected()
    record = await adapter.submit_order(_order(**overrides))
    assert record.final_status is OrderStatus.BROKER_REJECTED
    assert record.rejection_reason == reason
    assert fake.sent == []


async def test_order_check_failure_blocks_send() -> None:
    adapter, fake = await _connected()
    fake.next_check_retcode = FakeMT5.TRADE_RETCODE_NO_MONEY
    record = await adapter.submit_order(_order())
    assert record.rejection_reason.startswith("order_check_failed")
    assert fake.sent == []


async def test_only_approved_orders_are_executed() -> None:
    adapter, _ = await _connected()
    with pytest.raises(ValueError, match="risk-approved"):
        await adapter.submit_order(_order(status=OrderStatus.CREATED))


async def test_definite_broker_reject_is_recorded() -> None:
    adapter, fake = await _connected()
    fake.next_send_retcode = FakeMT5.TRADE_RETCODE_REQUOTE
    record = await adapter.submit_order(_order())
    assert record.final_status is OrderStatus.BROKER_REJECTED
    assert record.rejection_reason == "TRADE_RETCODE_REQUOTE"


async def test_partial_fill_is_reported() -> None:
    adapter, fake = await _connected()
    fake.next_send_retcode = FakeMT5.TRADE_RETCODE_DONE_PARTIAL
    record = await adapter.submit_order(_order())
    assert record.final_status is OrderStatus.PARTIALLY_FILLED


# ------------------------------------------------------- duplicates & crash safety


async def test_resubmitting_the_same_order_never_opens_a_second_position() -> None:
    adapter, fake = await _connected()
    order = _order()
    first = await adapter.submit_order(order)
    second = await adapter.submit_order(order)
    assert first.final_status is second.final_status is OrderStatus.FILLED
    assert second.mt5_response["duplicate_suppressed"] is True
    assert len(fake.sent) == 1 and len(fake.positions) == 1


async def test_uncertain_outcome_raises_then_reconciles_from_broker_truth() -> None:
    adapter, fake = await _connected()
    fake.next_send_retcode = FakeMT5.TRADE_RETCODE_TIMEOUT
    fake.record_position_on_uncertain = True  # the order actually went through
    order = _order()
    with pytest.raises(RuntimeError, match="uncertain"):
        await adapter.submit_order(order)
    states = await adapter.broker_order_states([order.order_id, "ord_unknown"])
    assert states == {order.order_id: OrderStatus.FILLED.value}


async def test_send_returning_none_is_treated_as_uncertain() -> None:
    adapter, fake = await _connected()
    fake.send_returns_none = True
    with pytest.raises(RuntimeError, match="None"):
        await adapter.submit_order(_order())


async def test_closed_trades_are_reported_closed() -> None:
    adapter, fake = await _connected()
    order = _order()
    await adapter.submit_order(order)
    ticket = fake.positions[0].ticket
    record = await adapter.close_position(str(ticket))
    assert record.final_status is OrderStatus.CLOSED
    assert fake.positions == []
    assert (await adapter.broker_order_states([order.order_id]))[order.order_id] == "closed"
    with pytest.raises(KeyError):
        await adapter.close_position(str(ticket))


# ---------------------------------------------------------------------- account


async def test_account_snapshot_has_real_daily_pnl_drawdown_and_exposure() -> None:
    adapter, fake = await _connected()
    await adapter.submit_order(_order(volume=0.10))
    fake.positions[0].price_current = 2500.0
    fake.deals.append(
        SimpleNamespace(
            type=FakeMT5.DEAL_TYPE_SELL,
            entry=FakeMT5.DEAL_ENTRY_OUT,
            magic=1,
            comment="x",
            profit=-30.0,
            commission=-1.0,
            swap=0.0,
            fee=0.0,
        )
    )
    fake.deals.append(  # deposits must not count as trading P/L
        SimpleNamespace(
            type=FakeMT5.DEAL_TYPE_BALANCE,
            entry=0,
            magic=0,
            comment="dep",
            profit=500.0,
            commission=0.0,
            swap=0.0,
            fee=0.0,
        )
    )
    fake.profit = -20.0
    snap = await adapter.get_account()
    assert snap.daily_pnl == pytest.approx(-31.0 - 20.0)  # realized + floating
    assert snap.drawdown_pct == pytest.approx(0.2)  # 20 / 10,000
    assert snap.exposure == pytest.approx(0.10 * 100.0 * 2500.0)  # contract size applied
    assert snap.open_positions == 1


# -------------------------------------------- full pipeline, money-correct risk


async def test_pipeline_uses_broker_spec_and_rejects_the_old_blind_spot() -> None:
    """0.10 lot gold with a $5 stop is $50 risk = 5% of $1,000. The old price-unit math
    saw $0.50 (0.05%) and approved it."""
    adapter, fake = await _connected(FakeMT5(balance=1_000.0))
    settings = _settings(max_position_size=0.10)
    risk = RiskEngine(settings=settings)
    manager = OrderManager(settings=settings, risk_engine=risk)
    account = await adapter.get_account()
    signal = StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2500.3,
        stop_loss=2495.3,
        take_profit=2510.0,
        confidence=0.9,
    )
    order, decision, record = await manager.process_signal(
        signal, RiskContext(account=account, proposed_volume=0.10), adapter
    )
    assert not decision.approved and "max_risk_per_trade" in decision.reasons
    assert record is None and fake.sent == []


async def test_pipeline_places_a_correctly_sized_trade() -> None:
    adapter, fake = await _connected(FakeMT5(balance=10_000.0))
    settings = _settings(max_position_size=0.10, max_exposure_pct=1000.0)
    risk = RiskEngine(settings=settings)
    manager = OrderManager(settings=settings, risk_engine=risk)
    account = await adapter.get_account()
    signal = StrategySignal(
        symbol="XAUUSD",
        direction=OrderSide.BUY,
        entry=2500.3,
        stop_loss=2498.3,
        take_profit=2510.0,
        confidence=0.9,
    )
    # $2 stop * $100/unit * 0.05 lot = $10 = 0.1% of $10,000
    order, decision, record = await manager.process_signal(
        signal, RiskContext(account=account, proposed_volume=0.05), adapter
    )
    assert decision.approved, decision.reasons
    assert order.status is OrderStatus.FILLED and len(fake.sent) == 1
