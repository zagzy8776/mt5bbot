"""Null strategy — safe default that never emits."""

from __future__ import annotations

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy, mid_price


class NullStrategy(Strategy):
    """Never emits signals. Used as a safe default / placeholder."""

    name = "null"
    description = "Safe no-op strategy that never emits signals."
    version = "1.0.0"

    def generate_signal(self, event: MarketDataEvent) -> StrategySignal | None:
        return None

    def calculate_entry(self, event: MarketDataEvent, direction: OrderSide) -> float | None:
        return mid_price(event)

    def calculate_stop_loss(self, entry: float, direction: OrderSide) -> float | None:
        return None

    def calculate_take_profit(self, entry: float, direction: OrderSide) -> float | None:
        return None

    def confidence(self, event: MarketDataEvent) -> float:
        return 0.0
