"""Pure indicator helpers for the strategy families.

Every function is deterministic, takes plain floats and returns ``None`` when there is not enough
data — a strategy must be able to say "not enough history" instead of acting on a half-computed
value. Nothing here looks at the future: values are computed from the sequence as given.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def ema(values: Sequence[float], period: int) -> float | None:
    """Exponential moving average of the last `period` values (None when too short)."""
    if period <= 0 or len(values) < period:
        return None
    window = list(values)[-period:]
    multiplier = 2.0 / (period + 1.0)
    value = window[0]
    for price in window[1:]:
        value = price * multiplier + value * (1.0 - multiplier)
    return value


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = list(values)[-period:]
    return sum(window) / len(window)


def rsi(values: Sequence[float], period: int) -> float | None:
    """Relative strength index. A pure uptrend returns 100.0, a pure downtrend 0.0."""
    if period <= 0 or len(values) < period + 1:
        return None
    window = list(values)[-(period + 1) :]
    gains = 0.0
    losses = 0.0
    for previous, current in zip(window, window[1:], strict=False):
        change = current - previous
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """True range series (needs the previous close, so it starts at the second bar)."""
    ranges: list[float] = []
    for index in range(1, min(len(highs), len(lows), len(closes))):
        high, low = highs[index], lows[index]
        previous_close = closes[index - 1]
        ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return ranges


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> float | None:
    """Average true range over the last `period` bars."""
    ranges = true_ranges(highs, lows, closes)
    if period <= 0 or len(ranges) < period:
        return None
    return sum(ranges[-period:]) / period


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    """Bounded 0..100 trend-strength reading from directional movement dominance.

    Not the full Wilder smoothing: a simplified ratio that behaves correctly on clean trends versus
    chop, which is all this family needs to decide "is there a trend worth following?".
    """
    count = min(len(highs), len(lows), len(closes))
    if period <= 0 or count < period + 1:
        return None
    plus = 0.0
    minus = 0.0
    ranges: list[float] = []
    for index in range(count - period, count):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus += up_move if up_move > down_move and up_move > 0 else 0.0
        minus += down_move if down_move > up_move and down_move > 0 else 0.0
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    total_range = sum(ranges)
    if total_range <= 0:
        return 0.0
    plus_di = 100.0 * plus / total_range
    minus_di = 100.0 * minus / total_range
    denominator = plus_di + minus_di
    if denominator <= 0:
        return 0.0
    return 100.0 * abs(plus_di - minus_di) / denominator


@dataclass(frozen=True)
class Bands:
    middle: float
    upper: float
    lower: float

    def width(self) -> float:
        return self.upper - self.lower


def bollinger(values: Sequence[float], period: int, deviations: float = 2.0) -> Bands | None:
    """Bollinger bands using the population standard deviation of the window."""
    if period <= 0 or len(values) < period:
        return None
    window = [float(v) for v in values[-period:]]
    middle = sum(window) / len(window)
    variance = sum((value - middle) ** 2 for value in window) / len(window)
    sigma = variance**0.5
    return Bands(
        middle=middle, upper=middle + deviations * sigma, lower=middle - deviations * sigma
    )


def swing_points(
    highs: Sequence[float], lows: Sequence[float], *, left: int = 2, right: int = 2
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed swing highs/lows as (index, price).

    A swing high needs ``left`` lower highs before it and ``right`` lower highs after it, so the
    point is only confirmed ``right`` bars later: the caller gets confirmation, never a prediction.
    """
    count = min(len(highs), len(lows))
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for index in range(left, count - right):
        window_highs = list(highs[index - left : index + right + 1])
        window_lows = list(lows[index - left : index + right + 1])
        if highs[index] == max(window_highs) and window_highs.count(highs[index]) == 1:
            swing_highs.append((index, float(highs[index])))
        if lows[index] == min(window_lows) and window_lows.count(lows[index]) == 1:
            swing_lows.append((index, float(lows[index])))
    return swing_highs, swing_lows


def aggregate_by_factor(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    factor: int,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Aggregate consecutive bars into a higher timeframe (``factor`` bars per bucket).

    Only *complete* buckets are returned, so a higher-timeframe reading never uses a bar that is
    still forming.
    """
    if factor <= 1:
        return list(opens), list(highs), list(lows), list(closes)
    buckets = min(len(opens), len(highs), len(lows), len(closes)) // factor
    out_opens: list[float] = []
    out_highs: list[float] = []
    out_lows: list[float] = []
    out_closes: list[float] = []
    for bucket in range(buckets):
        start = bucket * factor
        end = start + factor
        out_opens.append(opens[start])
        out_highs.append(max(highs[start:end]))
        out_lows.append(min(lows[start:end]))
        out_closes.append(closes[end - 1])
    return out_opens, out_highs, out_lows, out_closes


__all__ = [
    "Bands",
    "adx",
    "aggregate_by_factor",
    "atr",
    "bollinger",
    "ema",
    "rsi",
    "sma",
    "swing_points",
    "true_ranges",
]
