"""MAE/MFE excursion tracking.

Deterministic and causal: prices are observed strictly in chronological order and the tracker
never looks ahead. Adverse/favourable are defined relative to the position's own direction:

* BUY : adverse = entry - price, favourable = price - entry
* SELL: adverse = price - entry, favourable = entry - price
"""

from __future__ import annotations

from dataclasses import dataclass

from mt5_platform.common.enums import OrderSide


@dataclass
class ExcursionTracker:
    """Maximum adverse / favourable excursion of one position from entry to exit."""

    side: OrderSide
    entry: float
    mae: float = 0.0  # price units, always >= 0
    mfe: float = 0.0  # price units, always >= 0
    samples: int = 0
    last_price: float | None = None
    extreme_adverse_price: float | None = None
    extreme_favorable_price: float | None = None

    def observe(self, price: float | None) -> None:
        """Observe one price. Ignores unusable values instead of inventing them."""
        if price is None:
            return
        try:
            value = float(price)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        direction = 1.0 if self.side is OrderSide.BUY else -1.0
        move = direction * (value - self.entry)
        self.samples += 1
        self.last_price = value
        if move < 0:
            adverse = -move
            if adverse > self.mae:
                self.mae = adverse
                self.extreme_adverse_price = value
        elif move > 0 and move > self.mfe:
            self.mfe = move
            self.extreme_favorable_price = value

    def observe_high_low(self, high: float | None, low: float | None) -> None:
        """Observe a candle's extremes (intra-bar order is unknowable, so both are observed)."""
        if self.side is OrderSide.BUY:
            self.observe(low)
            self.observe(high)
        else:
            self.observe(high)
            self.observe(low)

    def mae_pct(self) -> float:
        return (self.mae / self.entry * 100.0) if self.entry else 0.0

    def mfe_pct(self) -> float:
        return (self.mfe / self.entry * 100.0) if self.entry else 0.0

    def restore(
        self,
        *,
        mae: float | None,
        mfe: float | None,
        samples: int | None = None,
        last_price: float | None = None,
    ) -> None:
        """Restore persisted extremes after a restart: extremes only ever grow."""
        self.mae = max(self.mae, float(mae or 0.0))
        self.mfe = max(self.mfe, float(mfe or 0.0))
        self.samples = max(self.samples, int(samples or 0))
        if last_price is not None:
            self.last_price = float(last_price)
