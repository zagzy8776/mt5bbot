"""Instrument specifications: the only correct way to turn price distance into money.

A price move is NOT money. On XAUUSD one lot is 100 oz, so a $1.00 move on 0.10 lot
is $10.00, not $0.10. Every risk/exposure calculation that touches real execution must
go through ``InstrumentSpec`` (values come from ``mt5.symbol_info()`` at runtime).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_EPS = 1e-9


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    contract_size: float
    tick_size: float
    tick_value: float  # account-currency P/L of one tick move on 1 lot
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    digits: int = 2

    def __post_init__(self) -> None:
        for name in ("contract_size", "tick_size", "tick_value", "volume_step", "volume_min"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"InstrumentSpec.{name} must be a positive finite number")
        if self.volume_max < self.volume_min:
            raise ValueError("volume_max must be >= volume_min")

    @property
    def value_per_price_unit(self) -> float:
        """Account-currency P/L for a 1.0 price move on 1 lot."""
        return self.tick_value / self.tick_size

    def risk_money(self, entry: float, stop: float, volume: float) -> float:
        """Money lost if price travels from entry to stop (ignores slippage/spread)."""
        return abs(entry - stop) * self.value_per_price_unit * volume

    def notional(self, price: float, volume: float) -> float:
        """Position notional in quote currency (≈ account currency for USD-quoted pairs)."""
        return price * self.contract_size * volume

    def volume_is_valid(self, volume: float) -> list[str]:
        """Return broker-rule violations for ``volume`` (empty list = valid)."""
        problems: list[str] = []
        if volume < self.volume_min - _EPS:
            problems.append("volume_below_min")
        if volume > self.volume_max + _EPS:
            problems.append("volume_above_max")
        steps = (volume - self.volume_min) / self.volume_step
        if abs(steps - round(steps)) > 1e-6:
            problems.append("volume_not_on_step")
        return problems

    def normalize_volume(self, volume: float) -> float:
        """Round DOWN to the broker's volume step (never up — never add risk)."""
        steps = math.floor((volume - self.volume_min) / self.volume_step + _EPS)
        if steps < 0:
            return 0.0
        normalized = self.volume_min + steps * self.volume_step
        return round(min(normalized, self.volume_max), 8)


# Reference specs for offline sizing/tests. Real runs use mt5.symbol_info() instead;
# always verify against your broker before trusting these.
DEFAULT_SPECS: dict[str, InstrumentSpec] = {
    "XAUUSD": InstrumentSpec(
        "XAUUSD", contract_size=100.0, tick_size=0.01, tick_value=1.0, digits=2
    ),
    "EURUSD": InstrumentSpec(
        "EURUSD", contract_size=100_000.0, tick_size=0.00001, tick_value=1.0, digits=5
    ),
}


def min_equity_for_min_lot(*, stop_distance: float, risk_pct: float, spec: InstrumentSpec) -> float:
    """Smallest equity at which the minimum lot with this stop distance fits ``risk_pct``."""
    if stop_distance <= 0 or risk_pct <= 0:
        raise ValueError("stop_distance and risk_pct must be positive")
    loss_at_min_lot = stop_distance * spec.value_per_price_unit * spec.volume_min
    return loss_at_min_lot / (risk_pct / 100.0)


def position_size_for_risk(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    spec: InstrumentSpec,
    max_volume: float | None = None,
) -> float:
    """Largest valid volume whose stop-out loss stays within ``risk_pct`` of equity.

    Returns ``0.0`` when even the minimum lot would risk more than the budget. That is
    the answer callers must respect: on a small account the correct trade is often *no
    trade*. Never round up to the minimum lot to "make it work".
    """
    if not (math.isfinite(equity) and math.isfinite(entry) and math.isfinite(stop)):
        return 0.0
    if equity <= 0 or risk_pct <= 0 or entry <= 0 or stop <= 0 or entry == stop:
        return 0.0
    risk_budget = equity * risk_pct / 100.0
    per_lot_risk = spec.risk_money(entry, stop, 1.0)
    if per_lot_risk <= 0:
        return 0.0
    raw = risk_budget / per_lot_risk
    if raw < spec.volume_min - _EPS:
        return 0.0
    cap = spec.volume_max if max_volume is None else min(spec.volume_max, max_volume)
    volume = spec.normalize_volume(min(raw, cap))
    return volume if volume >= spec.volume_min - _EPS else 0.0
