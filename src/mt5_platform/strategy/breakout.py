"""Donchian-style breakout strategy (structural example — not a profitability claim)."""

from __future__ import annotations

from collections import deque

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy, mid_price
from mt5_platform.strategy.mixins import PctRiskMixin


class BreakoutStrategy(PctRiskMixin, Strategy):
    """BUY when price breaks above lookback high; SELL below lookback low.

    Reference implementation only. Not validated as profitable.
    """

    name = "breakout"
    description = "BUY on a break above the lookback high; SELL on a break below the lookback low."
    version = "1.0.0"
    _parameter_names = ("lookback", "stop_loss_pct", "take_profit_pct")

    def __init__(
        self,
        *,
        lookback: int = 20,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        super().__init__(symbols=symbols)
        self.lookback = lookback
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._history: dict[str, deque[float]] = {}

    def reset(self) -> None:
        self._history.clear()

    def _window(self, symbol: str) -> deque[float]:
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self.lookback)
        return self._history[symbol]

    def confidence(self, event: MarketDataEvent) -> float:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return 0.0
        window = self._window(symbol)
        price = mid_price(event)
        if price is None or len(window) < self.lookback:
            return 0.0
        high = max(window)
        low = min(window)
        span = high - low
        if span <= 0:
            return 0.0
        if price > high:
            return max(0.0, min(1.0, (price - high) / span))
        if price < low:
            return max(0.0, min(1.0, (low - price) / span))
        return 0.0

    def generate_signal(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return None

        price = mid_price(event)
        if price is None:
            return None

        window = self._window(symbol)
        if len(window) < self.lookback:
            window.append(price)
            return None

        high = max(window)
        low = min(window)
        direction: OrderSide | None = None
        if price > high:
            direction = OrderSide.BUY
        elif price < low:
            direction = OrderSide.SELL

        window.append(price)
        if direction is None:
            return None

        entry = self.calculate_entry(event, direction)
        if entry is None:
            return None
        return StrategySignal(
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop_loss=self.calculate_stop_loss(entry, direction),
            take_profit=self.calculate_take_profit(entry, direction),
            confidence=self.confidence(event),
            reason=f"breakout_{direction.value} high={high:.4f} low={low:.4f}",
            timestamp=event.timestamp,
            strategy_name=self.name,
            correlation_id=event.correlation_id,
            metadata={
                "lookback": self.lookback,
                "range_high": high,
                "range_low": low,
                "disclaimer": "example_strategy_unvalidated",
            },
        )
