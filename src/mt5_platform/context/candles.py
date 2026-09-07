"""Rolling multi-timeframe candle builders fed directly from ticks."""

from __future__ import annotations

from collections import deque
from datetime import datetime

from mt5_platform.context.models import Candle

TF_LABELS: dict[int, str] = {
    60: "M1",
    300: "M5",
    900: "M15",
    3600: "H1",
    14400: "H4",
}


def timeframe_label(timeframe_s: int) -> str:
    if timeframe_s in TF_LABELS:
        return TF_LABELS[timeframe_s]
    if timeframe_s % 3600 == 0:
        return f"H{timeframe_s // 3600}"
    if timeframe_s % 60 == 0:
        return f"M{timeframe_s // 60}"
    return f"S{timeframe_s}"


class TimeframeCandleBuilder:
    """Aggregates ticks into fixed-timeframe candles with a rolling window.

    Only completed candles are exposed; the forming candle is excluded so
    feature extraction is deterministic for a given accepted-tick stream.
    """

    def __init__(self, timeframe_s: int, max_bars: int = 300) -> None:
        if timeframe_s <= 0:
            raise ValueError("timeframe_s must be positive")
        self.timeframe_s = timeframe_s
        self.label = timeframe_label(timeframe_s)
        self._max_bars = max_bars
        self._completed: deque[Candle] = deque(maxlen=max_bars)
        self._current: dict | None = None
        self._first_bucket: int | None = None
        self._last_bucket: int | None = None

    def update(self, ts: datetime, price: float, volume: float = 0.0) -> None:
        bucket = int(ts.timestamp()) // self.timeframe_s * self.timeframe_s
        if self._current is not None and bucket == self._current["bucket"]:
            cur = self._current
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
            cur["volume"] += volume
            cur["ticks"] += 1
            return
        if self._current is not None:
            self._completed.append(self._finish(self._current))
        self._current = {
            "bucket": bucket,
            "tz": ts.tzinfo,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
            "ticks": 1,
        }
        if self._first_bucket is None:
            self._first_bucket = bucket
        self._last_bucket = bucket

    @staticmethod
    def _finish(cur: dict) -> Candle:
        return Candle(
            timestamp=datetime.fromtimestamp(cur["bucket"], tz=cur["tz"]),
            open=cur["open"],
            high=cur["high"],
            low=cur["low"],
            close=cur["close"],
            volume=cur["volume"],
            tick_count=cur["ticks"],
        )

    def candles(self) -> list[Candle]:
        return list(self._completed)

    def gap_stats(self) -> tuple[int, float]:
        """Missing buckets and their ratio over the completed-candle span."""
        if self._first_bucket is None or self._last_bucket is None:
            return 0, 0.0
        expected = (self._last_bucket - self._first_bucket) // self.timeframe_s + 1
        missing = expected - len(self._completed) - (1 if self._current else 0)
        missing = max(0, missing)
        ratio = missing / expected if expected > 0 else 0.0
        return missing, ratio

    def __len__(self) -> int:
        return len(self._completed)


class MultiTimeframeCandleBuilder:
    """Feeds every configured timeframe from the same accepted tick stream."""

    def __init__(
        self, timeframes_s: tuple[int, ...], max_bars: int = 300
    ) -> None:
        if not timeframes_s:
            raise ValueError("at least one timeframe is required")
        if sorted(timeframes_s) != list(timeframes_s):
            raise ValueError("timeframes_s must be sorted ascending")
        self.builders = {
            tf: TimeframeCandleBuilder(tf, max_bars=max_bars) for tf in timeframes_s
        }

    def update(self, ts: datetime, price: float, volume: float = 0.0) -> None:
        for builder in self.builders.values():
            builder.update(ts, price, volume)

    def candles(self) -> dict[str, list[Candle]]:
        return {b.label: b.candles() for b in self.builders.values()}

    def builder(self, timeframe_s: int) -> TimeframeCandleBuilder:
        return self.builders[timeframe_s]

    def labels(self) -> list[str]:
        return [b.label for b in self.builders.values()]

    def insufficient_labels(self, min_bars: int) -> list[str]:
        return [
            b.label
            for b in self.builders.values()
            if len(b) < min_bars
        ]