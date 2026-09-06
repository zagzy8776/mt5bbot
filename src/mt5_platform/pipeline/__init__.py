"""Normalization and validation gates before trading engines see data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from mt5_platform.common.events import MarketDataEvent


@dataclass
class ValidationResult:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    event: MarketDataEvent | None = None


class MarketDataValidator:
    """Rejects stale, malformed, or jump-anomalous ticks from entering the engine."""

    def __init__(
        self,
        *,
        stale_max_age_ms: int = 5000,
        max_jump_pct: float = 5.0,
        last_prices: dict[str, float] | None = None,
    ) -> None:
        self.stale_max_age_ms = stale_max_age_ms
        self.max_jump_pct = max_jump_pct
        self._last_prices = last_prices or {}
        self._seen_keys: set[str] = set()

    def validate(self, event: MarketDataEvent, *, now: datetime | None = None) -> ValidationResult:
        reasons: list[str] = []
        now = now or datetime.now(UTC)

        if event.bid is None and event.ask is None and event.price is None:
            reasons.append("missing_price_fields")

        if event.bid is not None and event.ask is not None and event.bid > event.ask:
            reasons.append("bid_greater_than_ask")

        age_ms = (now - event.timestamp).total_seconds() * 1000
        if age_ms > self.stale_max_age_ms:
            reasons.append("stale_data")

        dedupe_key = (
            f"{event.source}:{event.symbol}:{event.timestamp.isoformat()}"
            f":{event.price}:{event.bid}:{event.ask}"
        )
        if dedupe_key in self._seen_keys:
            reasons.append("duplicate_event")
        else:
            self._seen_keys.add(dedupe_key)

        price = event.price
        if price is None and event.bid is not None and event.ask is not None:
            price = (event.bid + event.ask) / 2.0

        if price is not None:
            last = self._last_prices.get(event.symbol)
            if last and last > 0:
                jump_pct = abs(price - last) / last * 100.0
                if jump_pct > self.max_jump_pct:
                    reasons.append("sudden_price_jump")
            if "sudden_price_jump" not in reasons and "stale_data" not in reasons:
                self._last_prices[event.symbol] = price

        if reasons:
            return ValidationResult(accepted=False, reasons=reasons, event=None)
        return ValidationResult(accepted=True, reasons=[], event=event)
