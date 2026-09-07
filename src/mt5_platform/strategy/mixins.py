"""Shared strategy building blocks: entry price and percentage-based risk levels."""

from __future__ import annotations

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent
from mt5_platform.strategy.base import mid_price


class PctRiskMixin:
    """Implements entry at mid-price and percentage-based stop/take levels.

    `stop_loss_pct` / `take_profit_pct` are percentages (e.g. 0.5 = 0.5% offset
    from entry). Structural defaults only — never a profitability claim.
    """

    stop_loss_pct: float = 0.5
    take_profit_pct: float = 1.0

    def calculate_entry(self, event: MarketDataEvent, direction: OrderSide) -> float | None:
        return mid_price(event)

    def calculate_stop_loss(self, entry: float, direction: OrderSide) -> float | None:
        if entry is None:
            return None
        delta = entry * (self.stop_loss_pct / 100.0)
        return entry - delta if direction is OrderSide.BUY else entry + delta

    def calculate_take_profit(self, entry: float, direction: OrderSide) -> float | None:
        if entry is None:
            return None
        delta = entry * (self.take_profit_pct / 100.0)
        return entry + delta if direction is OrderSide.BUY else entry - delta
