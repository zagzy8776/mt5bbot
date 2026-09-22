"""Broker-rejection regression tests using the real Exness XAUUSDm contract values.

All values below were read from the LIVE demo terminal with read-only calls only
(``symbol_info`` / ``terminal_info`` / ``order_check``; order_check never sends an order):

    symbol XAUUSDm (Exness-MT5Trial9 demo, terminal build 6205)
      digits 3, point 0.001, trade_tick_size 0.001, trade_tick_value 0.1, contract size 100
      volume_min 0.01, volume_max 200.0, volume_step 0.01
      trade_stops_level 0, trade_freeze_level 0
      filling_mode 3 (FOK|IOC), trade_execution 2 (market), trade_mode 4 (full)
      tick bid 4333.194 / ask 4333.454 -> spread 260 points

Live findings these tests lock in:

* ``order_check`` for the exact request the adapter builds (BUY 0.02, SL -21.70, TP +43.41, IOC)
  answers retcode 0 "Done", margin ~17.33 → the request shape is broker-valid.
* ``order_check`` with ORDER_FILLING_RETURN answers 10030 "Unsupported filling mode".
* ``order_check`` with SL/TP 0.01 away answers 10016 "Invalid stops".
* with the terminal's AutoTrading off, every order_send is answered 10027
  (TRADE_RETCODE_CLIENT_DISABLES_AT) "AutoTrading disabled by client" — reproduced with a harmless
  TRADE_ACTION_SLTP probe against a non-existent position.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from mt5_platform.common.enums import AuditEventType, OrderSide, OrderStatus
from mt5_platform.common.events import OrderRequest
from mt5_platform.config import Settings
from mt5_platform.execution.mt5_adapter import TERMINAL_BLOCK_COMMENT, MT5ExecutionAdapter
from tests.fake_mt5 import FakeMT5

SYMBOL = "XAUUSDm"
ASK = 4333.454
BID = 4333.194
DIGITS = 3
POINT = 0.001
STOP_DISTANCE = 21.70
TARGET_DISTANCE = 43.41


class ExnessXauusdm(FakeMT5):
    """FakeMT5 carrying the real Exness XAUUSDm contract values probed live."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.symbol = SimpleNamespace(
            digits=DIGITS,
            point=POINT,
            spread=260,
            spread_float=True,
            trade_tick_size=0.001,
            trade_tick_value=0.1,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_max=200.0,
            volume_step=0.01,
            trade_stops_level=0,
            trade_freeze_level=0,
            filling_mode=3,
            trade_execution=2,
            trade_mode=4,
            order_mode=127,
            order_gtc_mode=0,
            expiration_mode=15,
            margin_initial=0.0,
            margin_maintenance=0.0,
            trade_calc_mode=0,
            visible=True,
        )
        self.tick = SimpleNamespace(bid=BID, ask=ASK, spread=260, time=1, time_msc=1000)

    def symbol_info(self, symbol):  # the broker symbol is case-sensitive, exactly as live
        return self.symbol if symbol == SYMBOL else None


def _order(**overrides: Any) -> OrderRequest:
    base: dict[str, Any] = {
        "symbol": SYMBOL,
        "side": OrderSide.BUY,
        "volume": 0.02,
        "entry": ASK,
        "stop_loss": round(ASK - STOP_DISTANCE, DIGITS),
        "take_profit": round(ASK + TARGET_DISTANCE, DIGITS),
        "status": OrderStatus.APPROVED,
        "created_at": datetime(2026, 9, 22, 13, 15, tzinfo=UTC),
    }
    base.update(overrides)
    return OrderRequest(**base)


def _adapter(fake: FakeMT5 | None = None, **settings_kwargs: Any) -> MT5ExecutionAdapter:
    settings = Settings(execution_backend="mt5", default_symbol=SYMBOL, **settings_kwargs)
    return MT5ExecutionAdapter(settings, client=fake or ExnessXauusdm())


# ------------------------------------------------------------------ request shape


async def test_request_shape_is_broker_valid_for_the_real_symbol() -> None:
    fake = ExnessXauusdm()
    adapter = _adapter(fake)
    await adapter.connect()

    record = await adapter.submit_order(_order())

    assert record.final_status is OrderStatus.FILLED
    sent = fake.sent[-1]
    assert sent["symbol"] == SYMBOL
    assert sent["volume"] == 0.02  # step 0.01, min 0.01, max 200
    assert sent["price"] == ASK  # a BUY crosses the spread at the ask
    assert sent["sl"] == round(ASK - STOP_DISTANCE, DIGITS)  # stops_level is 0 -> no extra margin
    assert sent["tp"] == round(ASK + TARGET_DISTANCE, DIGITS)
    assert sent["magic"] == adapter._settings.mt5_magic
    assert sent["type_time"] == adapter.mt5.ORDER_TIME_GTC
    assert sent["type_filling"] == adapter.mt5.ORDER_FILLING_IOC  # filling_mode 3 (FOK|IOC)
    assert sent["deviation"] == int(adapter._settings.max_slippage_points)


async def test_market_execution_symbol_never_gets_return_filling() -> None:
    """Probe-verified: RETURN on this market-execution symbol is answered 10030."""
    fake = ExnessXauusdm()
    fake.symbol.filling_mode = 4  # BOC advertised, no FOK/IOC flag
    fake.symbol.trade_execution = 2  # market execution
    adapter = _adapter(fake)
    assert adapter._filling_mode(fake.symbol) == adapter.mt5.ORDER_FILLING_IOC


async def test_return_filling_is_only_used_for_non_market_execution() -> None:
    fake = ExnessXauusdm()
    fake.symbol.filling_mode = 0
    fake.symbol.trade_execution = 1  # instant execution
    adapter = _adapter(fake)
    assert adapter._filling_mode(fake.symbol) == adapter.mt5.ORDER_FILLING_RETURN


# ------------------------------------------------------- terminal AutoTrading off


async def test_terminal_autotrading_off_blocks_submission_without_broker_traffic() -> None:
    from mt5_platform.common.audit import audit_log

    audit_log.clear()
    fake = ExnessXauusdm()
    fake.trade_allowed = False  # the AutoTrading button is off in the terminal
    adapter = _adapter(fake)
    await adapter.connect()

    record = await adapter.submit_order(_order())

    assert record.final_status is OrderStatus.BROKER_REJECTED
    assert record.rejection_reason == "terminal_autotrading_disabled"
    assert fake.sent == [], "a doomed order must never be transmitted"
    response = record.mt5_response
    assert response["transmitted"] is False
    assert response["retcode"] == "TRADE_RETCODE_CLIENT_DISABLES_AT"
    assert response["retcode_code"] == 10027
    assert response["comment"] == TERMINAL_BLOCK_COMMENT
    assert response["credentials_included"] is False
    assert response["terminal"]["trade_allowed"] is False
    assert response["symbol_spec"]["filling_mode"] == 3
    assert response["requested"]["volume"] == 0.02

    blocked = [
        e
        for e in audit_log.recent(50)
        if e.event_type == AuditEventType.EXECUTION_BLOCKED.value
    ]
    assert len(blocked) == 1, "one clear event, not one per attempt"
    assert blocked[0].payload["expected_retcode_code"] == 10027
    assert "AutoTrading" in blocked[0].payload["action_required"]


async def test_repeated_attempts_while_blocked_do_not_spam_the_broker_or_the_log() -> None:
    from mt5_platform.common.audit import audit_log

    audit_log.clear()
    fake = ExnessXauusdm()
    fake.trade_allowed = False
    adapter = _adapter(fake)
    await adapter.connect()
    assert adapter.execution_availability()["blocked"] is True  # known before any submission

    for _ in range(3):
        await adapter.submit_order(_order())

    assert fake.sent == []
    blocked = [
        e
        for e in audit_log.recent(50)
        if e.event_type == AuditEventType.EXECUTION_BLOCKED.value
    ]
    assert len(blocked) == 1
    availability = adapter.execution_availability()
    assert availability["blocked"] is True
    assert availability["block"]["reason"] == "terminal_autotrading_disabled"
    assert availability["block"]["attempts_while_blocked"] == 3


async def test_enabling_autotrading_clears_the_block_and_lets_the_order_through() -> None:
    from mt5_platform.common.audit import audit_log

    audit_log.clear()
    fake = ExnessXauusdm()
    fake.trade_allowed = False
    adapter = _adapter(fake)
    await adapter.connect()
    await adapter.submit_order(_order())
    assert adapter.execution_availability()["blocked"] is True

    fake.trade_allowed = True  # operator enables AutoTrading
    record = await adapter.submit_order(_order())

    assert record.final_status is OrderStatus.FILLED
    assert fake.sent, "the order is transmitted once trading is allowed again"
    assert adapter.execution_availability()["blocked"] is False
    unblocked = [
        e for e in audit_log.recent(50) if e.event_type == AuditEventType.EXECUTION_UNBLOCKED.value
    ]
    assert len(unblocked) == 1


# ------------------------------------------------------- genuine broker rejections


async def test_autotrading_state_is_published_at_connect_time() -> None:
    """The operator must see the block before the first signal is refused, not after."""
    fake = ExnessXauusdm()
    fake.trade_allowed = False
    adapter = _adapter(fake)

    await adapter.connect()

    availability = adapter.execution_availability()
    assert availability["blocked"] is True
    assert availability["terminal"]["trade_allowed"] is False
    assert availability["terminal"]["expected_retcode_code"] == 10027


async def test_race_where_the_broker_still_answers_10027_is_reported_exactly() -> None:
    """The terminal flag can flip between the pre-flight and the send; the answer must survive."""
    fake = ExnessXauusdm()
    fake.next_send_retcode = fake.TRADE_RETCODE_CLIENT_DISABLES_AT
    fake.send_comment = TERMINAL_BLOCK_COMMENT
    adapter = _adapter(fake)
    await adapter.connect()

    record = await adapter.submit_order(_order())

    assert record.final_status is OrderStatus.BROKER_REJECTED
    assert record.mt5_response["transmitted"] is True
    assert record.mt5_response["retcode"] == "TRADE_RETCODE_CLIENT_DISABLES_AT"
    assert record.mt5_response["retcode_code"] == 10027
    assert record.mt5_response["comment"] == "AutoTrading disabled by client"


async def test_unsupported_filling_mode_rejection_is_reported_exactly() -> None:
    fake = ExnessXauusdm()
    fake.next_send_retcode = fake.TRADE_RETCODE_INVALID_FILL
    fake.send_comment = "Unsupported filling mode"
    adapter = _adapter(fake)
    await adapter.connect()

    record = await adapter.submit_order(_order())

    assert record.final_status is OrderStatus.BROKER_REJECTED
    assert record.mt5_response["retcode"] == "TRADE_RETCODE_INVALID_FILL"
    assert record.mt5_response["comment"] == "Unsupported filling mode"
    assert record.rejection_reason == "TRADE_RETCODE_INVALID_FILL"  # code name, not a guess


async def test_invalid_stops_rejection_is_reported_exactly() -> None:
    fake = ExnessXauusdm()
    fake.next_send_retcode = fake.TRADE_RETCODE_INVALID_STOPS
    fake.send_comment = "Invalid stops"
    adapter = _adapter(fake)
    await adapter.connect()

    record = await adapter.submit_order(_order())

    assert record.mt5_response["retcode_code"] == 10016
    assert record.mt5_response["comment"] == "Invalid stops"


# ---------------------------------------------------------------- diagnostics block


async def test_diagnostics_are_complete_and_credential_free() -> None:
    fake = ExnessXauusdm()
    adapter = _adapter(fake, mt5_login="47123456", mt5_password="super-secret", mt5_server="Srv")
    await adapter.connect()

    record = await adapter.submit_order(_order())
    diagnostics = record.mt5_response["diagnostics"]

    assert set(diagnostics["requested"]) >= {
        "symbol",
        "side",
        "type",
        "volume",
        "price_sent",
        "stop_loss",
        "take_profit",
        "deviation_points",
        "deviation_price",
        "magic",
        "type_time",
    }
    assert set(diagnostics["market"]) == {"bid", "ask", "spread_points", "spread_price"}
    assert set(diagnostics["symbol_spec"]) >= {
        "digits",
        "point",
        "trade_tick_size",
        "trade_tick_value",
        "trade_contract_size",
        "volume_min",
        "volume_max",
        "volume_step",
        "trade_stops_level",
        "trade_freeze_level",
        "filling_mode",
        "resolved_filling_mode",
        "trade_execution",
        "trade_mode",
    }
    assert set(diagnostics["account"]) >= {"trade_allowed", "trade_expert", "leverage"}
    assert diagnostics["terminal"]["trade_allowed"] is True
    assert diagnostics["order_check"]["retcode"] == 0
    assert diagnostics["order_send"]["retcode"] == 10009
    assert diagnostics["credentials_included"] is False
    # The live symbol advertises a 260-point spread against a 25-point deviation: this is the
    # warning that explains a PRICE_OFF/REQUOTE answer without changing any configured limit.
    assert any("deviation_price_below_spread" in w for w in diagnostics["warnings"])
    flattened = repr(diagnostics)
    assert "super-secret" not in flattened and "47123456" not in flattened


# ------------------------------------------------- order / execution persistence


async def test_order_status_transitions_persist_in_place_with_the_rejection_reason() -> None:
    """Regression: insert-only persistence raised on every transition after the first write, so a
    broker rejection was never stored and the loop counted a cycle error instead."""
    from sqlalchemy import select

    from mt5_platform.storage.db import create_engine, create_session_factory, init_db
    from mt5_platform.storage.models import OrderRow
    from mt5_platform.storage.sqlalchemy_store import SqlAlchemyMarketDataStore

    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = create_session_factory(engine)
    store = SqlAlchemyMarketDataStore(factory)

    order = _order(status=OrderStatus.CREATED)
    await store.write_order(order)
    order.status = OrderStatus.APPROVED
    await store.write_order(order)  # second write of the same order_id
    order.status = OrderStatus.SUBMITTED
    order.metadata = {"status": order.status.value}
    await store.write_order(order)
    order.status = OrderStatus.BROKER_REJECTED
    order.rejection_reason = "TRADE_RETCODE_CLIENT_DISABLES_AT: AutoTrading disabled by client"
    order.metadata = {
        "status": order.status.value,
        "rejection_reason": order.rejection_reason,
    }
    await store.write_order(order)

    async with factory() as session:
        rows = (await session.scalars(select(OrderRow))).all()
    assert len(rows) == 1, "one row per order, updated in place"
    assert rows[0].status == OrderStatus.BROKER_REJECTED.value
    assert rows[0].metadata_json["rejection_reason"].startswith(
        "TRADE_RETCODE_CLIENT_DISABLES_AT"
    )
    await engine.dispose()


async def test_execution_persist_is_idempotent() -> None:
    from sqlalchemy import select

    from mt5_platform.storage.db import create_engine, create_session_factory, init_db
    from mt5_platform.storage.models import ExecutionRow
    from mt5_platform.storage.sqlalchemy_store import SqlAlchemyMarketDataStore

    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = create_session_factory(engine)
    store = SqlAlchemyMarketDataStore(factory)
    adapter = _adapter(ExnessXauusdm())
    await adapter.connect()
    record = await adapter.submit_order(_order())

    await store.write_execution(record)
    await store.write_execution(record)  # a retry must not raise or duplicate

    async with factory() as session:
        rows = (await session.scalars(select(ExecutionRow))).all()
    assert len(rows) == 1
    assert rows[0].mt5_response["diagnostics"]["symbol_spec"]["filling_mode"] == 3
    await engine.dispose()
