"""Z-score mean-reversion strategy (structural example — not a profitability claim)."""

from __future__ import annotations

from collections import deque
from statistics import mean, pstdev

from mt5_platform.common.enums import OrderSide
from mt5_platform.common.events import MarketDataEvent, StrategySignal
from mt5_platform.strategy.base import Strategy, mid_price
from mt5_platform.strategy.mixins import PctRiskMixin


class MeanReversionStrategy(PctRiskMixin, Strategy):
    """BUY when price is far below the rolling mean; SELL when far above it.

    Uses the z-score of the current price against the rolling window.
    Reference implementation only. Not validated as profitable.
    """

    name = "mean_reversion"
    description = "BUY when price falls z-scores below the rolling mean; SELL when it rises above."
    version = "1.0.0"
    _parameter_names = ("window", "threshold", "stop_loss_pct", "take_profit_pct")

    def __init__(
        self,
        *,
        window: int = 20,
        threshold: float = 2.0,
        stop_loss_pct: float = 0.5,
        take_profit_pct: float = 1.0,
        symbols: set[str] | None = None,
    ) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        if threshold <= 0:
            raise ValueError("threshold must be > 0")
        super().__init__(symbols=symbols)
        self.window = window
        self.threshold = threshold
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._prices: dict[str, deque[float]] = {}

    def reset(self) -> None:
        self._prices.clear()

    def _deque(self, symbol: str) -> deque[float]:
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=self.window)
        return self._prices[symbol]

    def _zscore(self, symbol: str) -> float | None:
        window = self._deque(symbol)
        if len(window) < self.window:
            return None
        mu = mean(window)
        sd = pstdev(window)
        if sd <= 0:
            return None
        return (window[-1] - mu) / sd

    def confidence(self, event: MarketDataEvent) -> float:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return 0.0
        z = self._zscore(symbol)
        if z is None:
            return 0.0
        return max(0.0, min(1.0, abs(z) / (2.0 * self.threshold)))

    def generate_signal(self, event: MarketDataEvent) -> StrategySignal | None:
        symbol = event.symbol.upper()
        if not self.handles(symbol):
            return None

        price = mid_price(event)
        if price is None:
            return None

        window = self._deque(symbol)
        window.append(price)
        z = self._zscore(symbol)
        if z is None:
            return None

        direction: OrderSide | None = None
        if z <= -self.threshold:
            direction = OrderSide.BUY
        elif z >= self.threshold:
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
            reason=f"mean_reversion_{direction.value} z={z:.2f}",
            timestamp=event.timestamp,
            strategy_name=self.name,
            correlation_id=event.correlation_id,
            metadata={
                "window": self.window,
                "threshold": self.threshold,
                "zscore": z,
                "disclaimer": "example_strategy_unvalidated",
            },
        )
