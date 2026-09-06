"""MT5 execution adapters. Adapters execute validated orders — they do not decide."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from mt5_platform.common.enums import OrderSide, OrderStatus
from mt5_platform.common.events import (
    AccountSnapshot,
    ExecutionRecord,
    OrderRequest,
    PositionInfo,
)
from mt5_platform.common.ids import new_execution_id
from mt5_platform.config import Settings


class ExecutionAdapter(ABC):
    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def is_connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def get_account(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def submit_order(self, order: OrderRequest) -> ExecutionRecord:
        raise NotImplementedError

    @abstractmethod
    async def reconcile(self) -> AccountSnapshot:
        """Always reconcile broker truth after restart before new trades."""
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> list[PositionInfo]:
        raise NotImplementedError

    @abstractmethod
    async def close_position(self, ticket: str) -> ExecutionRecord:
        raise NotImplementedError

    @abstractmethod
    async def broker_order_states(self, order_ids: list[str]) -> dict[str, str]:
        """Broker-side truth for the given internal order ids (reconciliation)."""
        raise NotImplementedError


class MockExecutionAdapter(ExecutionAdapter):
    """Deterministic paper adapter for unit/integration tests (no real orders)."""

    def __init__(
        self,
        *,
        starting_balance: float = 10_000.0,
        fill: bool = True,
        fill_ratio: float = 1.0,
        slippage: float = 0.0,
        rejection_reason: str = "mock_rejection",
        outcomes: Sequence[OrderStatus] = (),
        margin_rate: float = 0.01,
        point_value: float = 1.0,
    ) -> None:
        if not 0.0 < fill_ratio <= 1.0:
            raise ValueError("fill_ratio must be in (0.0, 1.0]")
        self._connected = False
        self._balance = starting_balance
        self._fill = fill
        self._fill_ratio = fill_ratio
        self._slippage = slippage
        self._rejection_reason = rejection_reason
        self._outcomes: list[OrderStatus] = list(outcomes)
        self._margin_rate = margin_rate
        self._point_value = point_value
        self.executions: list[ExecutionRecord] = []
        self.positions: dict[str, PositionInfo] = {}
        self._broker_orders: dict[str, OrderStatus] = {}
        self._market_prices: dict[str, float] = {}
        self._ticket_seq = 0

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    def set_market_price(self, symbol: str, price: float) -> None:
        """Feed a simulated market price for floating-P/L evaluation."""
        self._market_prices[symbol.strip().upper()] = price

    async def get_account(self) -> AccountSnapshot:
        floating = 0.0
        exposure = 0.0
        for pos in self.positions.values():
            price = self._market_prices.get(pos.symbol, pos.entry_price)
            direction = 1.0 if pos.side is OrderSide.BUY else -1.0
            pos.current_price = price
            pos.floating_pnl = (
                (price - pos.entry_price) * direction * pos.volume * self._point_value
            )
            floating += pos.floating_pnl
            exposure += pos.volume * pos.entry_price
        used_margin = exposure * self._margin_rate
        equity = self._balance + floating
        return AccountSnapshot(
            balance=self._balance,
            equity=equity,
            free_margin=equity - used_margin,
            used_margin=used_margin,
            margin_level=(equity / used_margin * 100.0) if used_margin > 0 else None,
            floating_pnl=floating,
            open_positions=len(self.positions),
            exposure=exposure,
        )

    async def submit_order(self, order: OrderRequest) -> ExecutionRecord:
        if not self._connected:
            raise RuntimeError("MockExecutionAdapter is not connected")
        if order.status not in {OrderStatus.APPROVED, OrderStatus.SUBMITTED}:
            raise ValueError("Only risk-approved/submitted orders may be executed")

        final_status = (
            self._outcomes.pop(0) if self._outcomes else self._default_outcome(order)
        )
        record = self._build_record(order, final_status)
        self.executions.append(record)
        self._broker_orders[order.order_id] = final_status
        if final_status in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
            self._open_position(order, record)
        return record

    def _default_outcome(self, order: OrderRequest) -> OrderStatus:
        if not self._fill:
            return OrderStatus.BROKER_REJECTED
        if self._fill_ratio < 1.0:
            return OrderStatus.PARTIALLY_FILLED
        return OrderStatus.FILLED

    def _fill_volume(
        self, order: OrderRequest, final_status: OrderStatus
    ) -> float | None:
        if final_status is OrderStatus.FILLED:
            return order.volume
        if final_status is OrderStatus.PARTIALLY_FILLED:
            return round(order.volume * self._fill_ratio, 2)
        return None

    def _build_record(
        self, order: OrderRequest, final_status: OrderStatus
    ) -> ExecutionRecord:
        filled = self._fill_volume(order, final_status)
        rejected = final_status is OrderStatus.BROKER_REJECTED
        direction = 1.0 if order.side is OrderSide.BUY else -1.0
        execution_price = (
            None
            if rejected or order.entry is None
            else order.entry + self._slippage * direction
        )
        slippage = (
            0.0
            if rejected or execution_price is None or order.entry is None
            else abs(execution_price - order.entry)
        )
        return ExecutionRecord(
            execution_id=new_execution_id(),
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            requested_volume=order.volume,
            requested_price=order.entry,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            mt5_response={
                "adapter": "mock",
                "ok": not rejected,
                "retcode": "TRADE_RETCODE_DONE"
                if not rejected
                else "TRADE_RETCODE_REJECT",
            },
            execution_price=execution_price,
            filled_volume=filled,
            slippage=slippage,
            rejection_reason=self._rejection_reason if rejected else None,
            final_status=final_status,
            correlation_id=order.correlation_id,
        )

    def _open_position(self, order: OrderRequest, record: ExecutionRecord) -> None:
        self._ticket_seq += 1
        ticket = f"mock_{self._ticket_seq}"
        self.positions[ticket] = PositionInfo(
            ticket=ticket,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            volume=record.filled_volume or order.volume,
            entry_price=record.execution_price or order.entry or 0.0,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
        )

    async def get_positions(self) -> list[PositionInfo]:
        await self.get_account()  # refresh current prices + floating P/L
        return list(self.positions.values())

    async def close_position(self, ticket: str) -> ExecutionRecord:
        if not self._connected:
            raise RuntimeError("MockExecutionAdapter is not connected")
        pos = self.positions.get(ticket)
        if pos is None:
            raise KeyError(f"unknown position: {ticket}")
        price = self._market_prices.get(pos.symbol, pos.entry_price)
        direction = 1.0 if pos.side is OrderSide.BUY else -1.0
        pnl = (price - pos.entry_price) * direction * pos.volume * self._point_value
        self._balance += pnl
        del self.positions[ticket]
        record = ExecutionRecord(
            execution_id=new_execution_id(),
            order_id=pos.order_id or "",
            symbol=pos.symbol,
            side=OrderSide.SELL if pos.side is OrderSide.BUY else OrderSide.BUY,
            requested_volume=pos.volume,
            requested_price=price,
            execution_price=price,
            filled_volume=pos.volume,
            slippage=0.0,
            mt5_response={
                "adapter": "mock",
                "ok": True,
                "action": "close",
                "realized_pnl": pnl,
            },
            final_status=OrderStatus.CLOSED,
            correlation_id=pos.ticket,
        )
        self.executions.append(record)
        return record

    async def broker_order_states(self, order_ids: list[str]) -> dict[str, str]:
        wanted = set(order_ids)
        return {
            oid: status.value
            for oid, status in self._broker_orders.items()
            if oid in wanted
        }

    async def reconcile(self) -> AccountSnapshot:
        return await self.get_account()


class MT5ExecutionAdapter(ExecutionAdapter):
    """Real MetaTrader5 bridge — Phase 7 (demo). Live mode is Phase 11 + gated."""

    async def connect(self) -> None:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")

    async def disconnect(self) -> None:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")

    async def is_connected(self) -> bool:
        return False

    async def get_account(self) -> AccountSnapshot:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")

    async def submit_order(self, order: OrderRequest) -> ExecutionRecord:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")

    async def get_positions(self) -> list[PositionInfo]:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")

    async def close_position(self, ticket: str) -> ExecutionRecord:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")

    async def broker_order_states(self, order_ids: list[str]) -> dict[str, str]:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")

    async def reconcile(self) -> AccountSnapshot:
        raise NotImplementedError("MT5 adapter arrives in Phase 7 (demo account only)")


def build_execution_adapter(settings: Settings) -> ExecutionAdapter:
    """Factory. The MT5 backend is Phase 7 (demo account only)."""
    backend = (settings.execution_backend or "mock").strip().lower()
    if backend == "mock":
        return MockExecutionAdapter(
            starting_balance=settings.mock_starting_balance,
            fill_ratio=settings.mock_fill_ratio,
            slippage=settings.mock_slippage,
        )
    if backend == "mt5":
        return MT5ExecutionAdapter()
    raise ValueError(f"unknown execution backend: {backend}")


__all__ = [
    "ExecutionAdapter",
    "MockExecutionAdapter",
    "MT5ExecutionAdapter",
    "build_execution_adapter",
]
