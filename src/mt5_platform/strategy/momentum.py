"""Lookback momentum strategy (structural example — not a profitability claim)."""

from __future__ import annotations

from collections import deque

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy, mid_price
from mt5_platform.strategy.mixins import PctRiskMixin


class MomentumStrategy(PctRiskMixin, Strategy):
    """BUY when lookback momentum crosses a positive threshold; SELL below negative.

    Momentum is the percent change from the oldest to the newest price in the
    lookback window. Reference implementation only. Not validated as profitable.
    """

    name = "momentum"
    description = "BUY when lookback momentum crosses above a positive threshold; SELL below it."
    version = "1.0.0"
    _parameter_names = ("lookback", "threshold_pct", "stop_loss_pct", "take_profit_pct")

    def __init__(
        self,
        *,
        lookback: int = 10,
        threshold_pct: float = 1.0,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if threshold_pct <= 0:
            raise ValueError("threshold_pct must be > 0")
        super().__init__(symbols=symbols)
        self.lookback = lookback
        self.threshold_pct = threshold_pct
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._prices: dict[str, deque[float]] = {}
        self._prev_momentum: dict[str, float | None] = {}

    def reset(self) -> None:
        self._prices.clear()
        self._prev_momentum.clear()

    def _deque(self, symbol: str) -> deque[float]:
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=self.lookback)
        return self._prices[symbol]

    def _momentum_pct(self, symbol: str) -> float | None:
        window = self._deque(symbol)
        if len(window) < self.lookback:
            return None
        oldest = window[0]
        current = window[-1]
        if oldest == 0:
            return None
        return (current - oldest) / oldest * 100.0

    def confidence(self, event: MarketDataEvent) -> float:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return 0.0
        momentum = self._momentum_pct(symbol)
        if momentum is None:
            return 0.0
        return max(0.0, min(1.0, abs(momentum) / (2.0 * self.threshold_pct)))

    def generate_signal(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return None

        price = mid_price(event)
        if price is None:
            return None

        window = self._deque(symbol)
        window.append(price)
        momentum = self._momentum_pct(symbol)
        if momentum is None:
            return None

        prev = self._prev_momentum.get(symbol)
        self._prev_momentum[symbol] = momentum

        direction: OrderSide | None = None
        if momentum >= self.threshold_pct and (prev is None or prev < self.threshold_pct):
            direction = OrderSide.BUY
        elif momentum <= -self.threshold_pct and (prev is None or prev > -self.threshold_pct):
            direction = OrderSide.SELL

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
            reason=f"momentum_{direction.value} mom={momentum:.2f}%",
            timestamp=event.timestamp,
            strategy_name=self.name,
            correlation_id=event.correlation_id,
            metadata={
                "lookback": self.lookback,
                "threshold_pct": self.threshold_pct,
                "momentum_pct": momentum,
                "disclaimer": "example_strategy_unvalidated",
            },
        )
