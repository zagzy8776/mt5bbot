"""Out-of-order and gap handling helpers for the market-data pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mt5_platform.common.events import MarketDataEvent


@dataclass
class OrderedEventBuffer:
    """Buffers slightly out-of-order events per symbol; drops late arrivals beyond skew."""

    max_skew_ms: float = 2000.0
    _last_ts: dict[str, datetime] = field(default_factory=dict)
    _buffer: dict[str, list[MarketDataEvent]] = field(default_factory=dict)

    def push(self, event: MarketDataEvent) -> list[MarketDataEvent]:
        """Insert event and return any events that are now safe to emit in order."""
        symbol = event.symbol
        bucket = self._buffer.setdefault(symbol, [])
        bucket.append(event)
        bucket.sort(key=lambda e: e.timestamp)

        last = self._last_ts.get(symbol)
        ready: list[MarketDataEvent] = []
        remaining: list[MarketDataEvent] = []

        for item in bucket:
            if last is None or item.timestamp >= last:
                ready.append(item)
                last = item.timestamp
                continue

            skew_ms = (last - item.timestamp).total_seconds() * 1000.0
            if skew_ms <= self.max_skew_ms:
                # Still within skew window but earlier than last — keep buffered.
                remaining.append(item)
            # else: drop as too-late / duplicate timeline noise

        self._buffer[symbol] = remaining
        if ready:
            self._last_ts[symbol] = ready[-1].timestamp
        return ready
