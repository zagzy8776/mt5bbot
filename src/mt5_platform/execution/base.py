"""Execution adapter contract. Adapters execute validated orders — they do not decide."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mt5_platform.common.events import (
    AccountSnapshot,
    ExecutionRecord,
    OrderRequest,
    PositionInfo,
)
from mt5_platform.common.instruments import InstrumentSpec


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
        """All broker positions, including externally/manual opened positions."""
        raise NotImplementedError

    @abstractmethod
    async def close_position(self, ticket: str, *, volume: float | None = None) -> ExecutionRecord:
        raise NotImplementedError

    async def modify_position(
        self,
        ticket: str,
        *,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> ExecutionRecord:
        raise NotImplementedError

    async def position_close_details(self, ticket: str) -> dict[str, Any] | None:
        """Broker truth for a position that is already closed (read-only, best effort).

        Used by the outcome recorder so a close the runtime did not perform (broker stop/target,
        manual close, close while offline) still records real money instead of "unavailable".
        Returns None when the broker cannot supply it — never a guessed value.
        """
        return None

    @abstractmethod
    async def broker_order_states(self, order_ids: list[str]) -> dict[str, str]:
        """Broker-side truth for the given internal order ids (reconciliation)."""
        raise NotImplementedError

    async def get_instrument(self, symbol: str) -> InstrumentSpec | None:
        """Broker contract spec for ``symbol`` (None = unknown / legacy price-unit math)."""
        return None
