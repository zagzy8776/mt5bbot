"""Candle-shape features: what the closed candles looked like at decision time.

Deterministic by construction — every value comes from the candles the runtime actually had, in
the order it had them, and stays ``None`` when there is not enough history. Nothing is inferred
from the future and nothing is invented; a missing measurement is reported as missing.

These features are captured (and versioned) with every outcome so later research can ask questions
such as "do entries on long-bodied breakout candles hold better than doji entries?" without ever
having to trust a reconstruction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

CANDLE_FEATURE_VERSION = "1.0"
DEFAULT_WINDOW = 20
_FLAT_EPSILON = 1e-12


def _value(candle: Any, name: str) -> float | None:
    """Read one OHLCV field from a Bar, a dict or any attribute holder."""
    raw = candle.get(name) if isinstance(candle, dict) else getattr(candle, name, None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CandleShapeFeatures:
    """Shape of the most recent closed candle plus the short-term context around it."""

    version: str
    candles_used: int
    direction: str  # bull | bear | flat | unknown
    pattern: str  # marubozu | long_body | doji | hammer | shooting_star | normal | unknown
    body_ratio: float | None
    upper_wick_ratio: float | None
    lower_wick_ratio: float | None
    close_position_in_range: float | None
    range_vs_window_median: float | None
    gap_pct: float | None
    consecutive_same_direction: int
    volume_ratio: float | None
    window_high: float | None
    window_low: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "candles_used": self.candles_used,
            "direction": self.direction,
            "pattern": self.pattern,
            "body_ratio": self.body_ratio,
            "upper_wick_ratio": self.upper_wick_ratio,
            "lower_wick_ratio": self.lower_wick_ratio,
            "close_position_in_range": self.close_position_in_range,
            "range_vs_window_median": self.range_vs_window_median,
            "gap_pct": self.gap_pct,
            "consecutive_same_direction": self.consecutive_same_direction,
            "volume_ratio": self.volume_ratio,
            "window_high": self.window_high,
            "window_low": self.window_low,
        }


def _classify(
    *, body_ratio: float, upper_wick: float, lower_wick: float, direction: str, range_ratio: float
) -> str:
    """Deliberately coarse, rule-based labels — no lookahead, no fitting.

    A dominant single wick is checked before the doji rule: a hammer/shooting star is *defined* by a
    small body plus one long wick, so treating any small body as a doji first would hide them.
    """
    if direction == "flat":
        return "doji"
    if lower_wick >= 0.6 and upper_wick <= 0.2:
        return "hammer"
    if upper_wick >= 0.6 and lower_wick <= 0.2:
        return "shooting_star"
    if body_ratio <= 0.05:
        return "doji"
    if body_ratio >= 0.85:
        return "marubozu" if range_ratio >= 1.0 else "long_body"
    if body_ratio >= 0.6:
        return "long_body"
    return "normal"


def compute_candle_shape(
    candles: Sequence[Any] | None, *, window: int = DEFAULT_WINDOW
) -> CandleShapeFeatures | None:
    """Shape features of the last closed candle, or None when no usable candle exists."""
    if not candles:
        return None
    usable = [c for c in candles if _value(c, "close") is not None][-max(1, window) :]
    if not usable:
        return None
    last = usable[-1]
    open_p = _value(last, "open")
    high = _value(last, "high")
    low = _value(last, "low")
    close = _value(last, "close")
    if None in (open_p, high, low, close):
        return None

    span = float(high) - float(low)
    if span <= _FLAT_EPSILON:
        body_ratio = 0.0
        upper_wick_ratio = 0.0
        lower_wick_ratio = 0.0
        close_position = 0.5
    else:
        body_ratio = abs(float(close) - float(open_p)) / span
        upper_wick_ratio = (float(high) - max(float(open_p), float(close))) / span
        lower_wick_ratio = (min(float(open_p), float(close)) - float(low)) / span
        close_position = (float(close) - float(low)) / span

    direction = (
        "bull"
        if float(close) > float(open_p)
        else "bear"
        if float(close) < float(open_p)
        else "flat"
    )

    ranges: list[float] = []
    for candle in usable[:-1]:
        candle_high, candle_low = _value(candle, "high"), _value(candle, "low")
        if candle_high is not None and candle_low is not None and candle_high > candle_low:
            ranges.append(candle_high - candle_low)
    median_range = median(ranges) if ranges else None
    range_ratio = (span / median_range) if median_range and median_range > 0 else None

    volumes: list[float] = []
    for candle in usable[:-1]:
        candle_volume = _value(candle, "volume")
        if candle_volume is not None and candle_volume > 0:
            volumes.append(candle_volume)
    last_volume = _value(last, "volume")
    median_volume = median(volumes) if volumes else None
    volume_ratio = (
        float(last_volume) / median_volume
        if last_volume is not None and median_volume and median_volume > 0
        else None
    )

    previous_close = _value(usable[-2], "close") if len(usable) >= 2 else None
    gap_pct = (
        (float(open_p) - float(previous_close)) / float(previous_close) * 100.0
        if previous_close not in (None, 0)
        else None
    )

    consecutive = 0
    for candle in reversed(usable):
        candle_open, candle_close = _value(candle, "open"), _value(candle, "close")
        if candle_open is None or candle_close is None:
            break
        candle_direction = (
            "bull"
            if candle_close > candle_open
            else "bear"
            if candle_close < candle_open
            else "flat"
        )
        if candle_direction != direction:
            break
        consecutive += 1

    highs = [h for h in (_value(c, "high") for c in usable) if h is not None]
    lows = [low_ for low_ in (_value(c, "low") for c in usable) if low_ is not None]
    return CandleShapeFeatures(
        version=CANDLE_FEATURE_VERSION,
        candles_used=len(usable),
        direction=direction,
        pattern=_classify(
            body_ratio=body_ratio,
            upper_wick=upper_wick_ratio,
            lower_wick=lower_wick_ratio,
            direction=direction,
            range_ratio=range_ratio if range_ratio is not None else 1.0,
        ),
        body_ratio=round(body_ratio, 6),
        upper_wick_ratio=round(upper_wick_ratio, 6),
        lower_wick_ratio=round(lower_wick_ratio, 6),
        close_position_in_range=round(close_position, 6),
        range_vs_window_median=round(range_ratio, 6) if range_ratio is not None else None,
        gap_pct=round(gap_pct, 6) if gap_pct is not None else None,
        consecutive_same_direction=consecutive,
        volume_ratio=round(volume_ratio, 6) if volume_ratio is not None else None,
        window_high=max(highs) if highs else None,
        window_low=min(lows) if lows else None,
    )


__all__ = ["CANDLE_FEATURE_VERSION", "CandleShapeFeatures", "compute_candle_shape"]
