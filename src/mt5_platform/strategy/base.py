"""Strategy interface. Implementations emit StrategySignal only — never orders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal

if TYPE_CHECKING:
    from mt5_platform.context import MarketContext


def mid_price(event: MarketDataEvent) -> float | None:
    if event.price is not None:
        return event.price
    if event.bid is not None and event.ask is not None:
        return (event.bid + event.ask) / 2.0
    return event.bid if event.bid is not None else event.ask


class Strategy(ABC):
    """Pluggable strategy contract.

    Signals are intents, not orders. RiskEngine must approve before OrderManager.
    Do not claim profitability without measured backtest/forward-test evidence.
    """

    name: str = "base"
    description: str = ""
    version: str = "1.0.0"
    enabled: bool = True
    # Subclasses declare the instance parameter names surfaced by `parameters()`.
    _parameter_names: tuple[str, ...] = ()

    def __init__(self, *, symbols: Iterable[str] | None = None) -> None:
        self.symbols: set[str] | None = {s.upper() for s in symbols} if symbols else None

    @abstractmethod
    def generate_signal(self, event: MarketDataEvent) -> StrategySignal | None:
        raise NotImplementedError

    @abstractmethod
    def calculate_entry(self, event: MarketDataEvent, direction: OrderSide) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def calculate_stop_loss(self, entry: float, direction: OrderSide) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def calculate_take_profit(self, entry: float, direction: OrderSide) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def confidence(self, event: MarketDataEvent) -> float:
        raise NotImplementedError

    def handles(self, symbol: str) -> bool:
        """True when this strategy is scoped to (or unrestricted for) the symbol."""
        return self.symbols is None or symbol.upper() in self.symbols

    def parameters(self) -> dict:
        """Serialized instance parameters (excludes symbol scope, shown separately)."""
        return {name: getattr(self, name) for name in self._parameter_names}

    def info(self) -> dict:
        """Serializable description for APIs / dashboards."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled,
            "symbols": sorted(self.symbols) if self.symbols else None,
            "parameters": self.parameters(),
        }

    def reset(self) -> None:
        """Clear internal state (tests / symbol rotation)."""
        return None

    def generate_from_context(self, context: MarketContext) -> StrategySignal | None:
        """Context-aware hook (Phase A+).

        Receives the canonical MarketContext so strategies never re-derive
        market state themselves. Default: not context-aware (legacy tick-based
        strategies keep working via generate_signal as candidate generators).
        """
        return None
