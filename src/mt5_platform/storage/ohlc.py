"""OHLC candle helpers for historical analysis and future backtests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mt5_platform.common.events import MarketDataEvent


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    tick_count: int
    spread_avg: float | None = None


_TIMEFRAME_DELTAS = {
    "1s": timedelta(seconds=1),
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


def bucket_start(ts: datetime, timeframe: str) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    else:
        ts = ts.astimezone(UTC)

    if timeframe not in _TIMEFRAME_DELTAS:
        raise ValueError(f"unsupported timeframe: {timeframe}")

    if timeframe == "1s":
        return ts.replace(microsecond=0)
    if timeframe == "1m":
        return ts.replace(second=0, microsecond=0)
    if timeframe == "5m":
        minute = ts.minute - (ts.minute % 5)
        return ts.replace(minute=minute, second=0, microsecond=0)
    if timeframe == "15m":
        minute = ts.minute - (ts.minute % 15)
        return ts.replace(minute=minute, second=0, microsecond=0)
    if timeframe == "1h":
        return ts.replace(minute=0, second=0, microsecond=0)
    if timeframe == "4h":
        hour = ts.hour - (ts.hour % 4)
        return ts.replace(hour=hour, minute=0, second=0, microsecond=0)
    # 1d
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _tick_price(event: MarketDataEvent) -> float | None:
    if event.price is not None:
        return event.price
    if event.bid is not None and event.ask is not None:
        return (event.bid + event.ask) / 2.0
    return event.bid if event.bid is not None else event.ask


def aggregate_ohlc(events: list[MarketDataEvent], *, timeframe: str = "1m") -> list[Candle]:
    """Aggregate ticks into OHLC candles. Empty input → empty output."""
    buckets: dict[tuple[str, datetime], list[MarketDataEvent]] = {}
    for event in events:
        key = (event.symbol, bucket_start(event.timestamp, timeframe))
        buckets.setdefault(key, []).append(event)

    candles: list[Candle] = []
    for (symbol, start), ticks in sorted(
        buckets.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        prices: list[float] = []
        volume = 0.0
        spreads: list[float] = []
        for tick in sorted(ticks, key=lambda t: t.timestamp):
            price = _tick_price(tick)
            if price is None:
                continue
            prices.append(price)
            volume += float(tick.volume or 0.0)
            if tick.spread is not None:
                spreads.append(tick.spread)
            elif tick.bid is not None and tick.ask is not None:
                spreads.append(tick.ask - tick.bid)
        if not prices:
            continue
        candles.append(
            Candle(
                timestamp=start,
                symbol=symbol,
                timeframe=timeframe,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=volume,
                tick_count=len(prices),
                spread_avg=(sum(spreads) / len(spreads)) if spreads else None,
            )
        )
    return candles
