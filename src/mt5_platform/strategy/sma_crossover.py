"""SMA crossover strategy (structural example — not a profitability claim)."""

from __future__ import annotations

from collections import deque

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy, mid_price
from mt5_platform.strategy.mixins import PctRiskMixin


class SmaCrossoverStrategy(PctRiskMixin, Strategy):
    """Emits BUY when fast SMA crosses above slow SMA; SELL on cross below.

    This is a reference implementation for the strategy interface.
    It has not been validated as profitable.
    """

    name = "sma_crossover"
    description = "BUY when the fast SMA crosses above the slow SMA; SELL on the cross below."
    version = "1.0.0"
    _parameter_names = ("fast_period", "slow_period", "stop_loss_pct", "take_profit_pct")

    def __init__(
        self,
        *,
        fast_period: int = 5,
        slow_period: int = 20,
        stop_loss_pct: float = 0.4,
        take_profit_pct: float = 0.8,
        symbols: set[str] | None = None,
    ) -> None:
        if fast_period < 1 or slow_period <= fast_period:
            raise ValueError("require 1 <= fast_period < slow_period")
        super().__init__(symbols=symbols)
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._prices: dict[str, deque[float]] = {}
        self._prev_fast_above: dict[str, bool | None] = {}

    def reset(self) -> None:
        self._prices.clear()
        self._prev_fast_above.clear()

    def _window(self, symbol: str) -> deque[float]:
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=self.slow_period)
        return self._prices[symbol]

    def _sma(self, values: deque[float], period: int) -> float | None:
        if len(values) < period:
            return None
        tail = list(values)[-period:]
        return sum(tail) / period

    def confidence(self, event: MarketDataEvent) -> float:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return 0.0
        window = self._window(symbol)
        if len(window) < self.slow_period:
            return 0.0
        fast = self._sma(window, self.fast_period)
        slow = self._sma(window, self.slow_period)
        if fast is None or slow is None or slow == 0:
            return 0.0
        sep = abs(fast - slow) / abs(slow)
        return max(0.0, min(1.0, sep * 50.0))

    def generate_signal(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return None

        price = mid_price(event)
        if price is None:
            return None

        window = self._window(symbol)
        window.append(price)
        fast = self._sma(window, self.fast_period)
        slow = self._sma(window, self.slow_period)
        if fast is None or slow is None:
            return None

        fast_above = fast > slow
        prev = self._prev_fast_above.get(symbol)
        self._prev_fast_above[symbol] = fast_above
        if prev is None or prev == fast_above:
            return None

        direction = OrderSide.BUY if fast_above else OrderSide.SELL
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
            reason=f"sma_cross_{'up' if fast_above else 'down'} fast={fast:.4f} slow={slow:.4f}",
            timestamp=event.timestamp,
            strategy_name=self.name,
            correlation_id=event.correlation_id,
            metadata={
                "fast_period": self.fast_period,
                "slow_period": self.slow_period,
                "fast_sma": fast,
                "slow_sma": slow,
                "disclaimer": "example_strategy_unvalidated",
            },
        )
