"""Live market data straight from the MT5 terminal (the price you actually trade on)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from mt5_platform.backtest.data import Bar, timeframe_minutes


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    spread_points: float
    age_ms: float  # how long the tick has been unchanged (stale market/feed detector)


class MarketFeed(Protocol):
    async def latest_closed_bar(self, symbol: str) -> Bar | None: ...
    async def history(self, symbol: str, count: int) -> list[Bar]: ...
    async def quote(self, symbol: str) -> Quote | None: ...


def _row_to_bar(r: Any) -> Bar:
    return Bar(
        datetime.fromtimestamp(int(r["time"]), UTC),
        float(r["open"]),
        float(r["high"]),
        float(r["low"]),
        float(r["close"]),
        float(r["tick_volume"]),
        float(r["spread"]),
    )


class MT5CandleFeed:
    """Closed candles + quotes. Uses the adapter's serialized call path (MT5 is not
    thread-safe). Tick staleness is measured by *change tracking*, because broker server
    time is not UTC and cannot be compared to the local clock."""

    def __init__(
        self, adapter: Any, timeframe: str, *, clock: Callable[[], float] = time.monotonic
    ):
        timeframe_minutes(timeframe)
        self._adapter = adapter
        self._tf_name = timeframe.upper()
        self._clock = clock
        self._last_tick: dict[str, tuple[Any, float]] = {}

    @property
    def timeframe(self) -> str:
        """Configured timeframe (upper-case), e.g. M15."""
        return self._tf_name

    def _tf(self) -> int:
        return getattr(self._adapter.mt5, f"TIMEFRAME_{self._tf_name}")

    async def history(self, symbol: str, count: int) -> list[Bar]:
        # start_pos=1 skips the still-forming bar 0, so every returned bar is closed.
        rates = await self._adapter.call("copy_rates_from_pos", symbol, self._tf(), 1, count)
        return [_row_to_bar(r) for r in rates] if rates is not None else []

    async def latest_closed_bar(self, symbol: str) -> Bar | None:
        bars = await self.history(symbol, 1)
        return bars[-1] if bars else None

    async def quote(self, symbol: str) -> Quote | None:
        tick = await self._adapter.call("symbol_info_tick", symbol)
        info = await self._adapter.call("symbol_info", symbol)
        if tick is None or info is None or not float(tick.bid) or not float(tick.ask):
            return None
        stamp = getattr(tick, "time_msc", None) or tick.time
        now = self._clock()
        prev = self._last_tick.get(symbol)
        if prev is None or prev[0] != stamp:
            self._last_tick[symbol] = (stamp, now)
            age = 0.0
        else:
            age = (now - prev[1]) * 1000.0
        point = float(info.point) or 0.01
        return Quote(
            float(tick.bid), float(tick.ask), (float(tick.ask) - float(tick.bid)) / point, age
        )
