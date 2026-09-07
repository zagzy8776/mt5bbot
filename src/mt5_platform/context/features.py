"""Deterministic, measurable feature extraction from candle windows.

Pure functions: same candles in, same features out. No randomness, no IO.
"""

from __future__ import annotations

from mt5_platform.context.models import (
    BreakoutState,
    Candle,
    MomentumFeatures,
    StructureFeatures,
    SupportResistanceLevel,
    TrendFeatures,
    VolatilityFeatures,
)

MIN_FEATURE_BARS = 20


def _percentile_rank(values: list[float], current: float) -> float | None:
    """Mid-rank percentile: a constant series ranks at 50, not 0 or 100."""
    if not values:
        return None
    below = sum(1 for v in values if v < current)
    equal = sum(1 for v in values if v == current)
    return (below + 0.5 * equal) / len(values) * 100.0


def _linear_slope(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    cov = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(values))
    var = sum((i - mean_x) ** 2 for i in range(n))
    if var == 0:
        return None
    return cov / var


def true_ranges(candles: list[Candle]) -> list[float]:
    """True range per candle; the first TR is simply high - low."""
    trs: list[float] = []
    prev_close: float | None = None
    for c in candles:
        base = c.high - c.low
        if prev_close is None:
            trs.append(base)
        else:
            trs.append(max(base, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return trs


def compute_trend(candles: list[Candle]) -> TrendFeatures | None:
    if len(candles) < MIN_FEATURE_BARS:
        return None
    closes = [c.close for c in candles]
    mean_price = sum(closes) / len(closes)
    if mean_price <= 0:
        return None
    slope = _linear_slope(closes)
    slope_pct = slope / mean_price * 100.0 if slope is not None else None
    net = (closes[-1] - closes[0]) / closes[0] * 100.0 if closes[0] > 0 else None
    changes = [abs(closes[i + 1] - closes[i]) for i in range(len(closes) - 1)]
    path = sum(changes)
    efficiency = abs(closes[-1] - closes[0]) / path if path > 0 else 0.0
    return TrendFeatures(
        slope_per_bar_pct=slope_pct,
        efficiency_ratio=efficiency,
        structure_score=None,  # filled by compute_structure
        net_change_pct=net,
    )


def compute_volatility(candles: list[Candle], atr_period: int = 14) -> VolatilityFeatures | None:
    if len(candles) < atr_period + 1:
        return None
    trs = true_ranges(candles)
    atr_series = [
        sum(trs[i - atr_period + 1 : i + 1]) / atr_period for i in range(atr_period - 1, len(trs))
    ]
    atr = atr_series[-1]
    last_close = candles[-1].close
    window_high = max(c.high for c in candles)
    window_low = min(c.low for c in candles)
    ordered = sorted(atr_series)
    median = ordered[len(ordered) // 2]
    return VolatilityFeatures(
        atr=atr,
        atr_percentile=_percentile_rank(atr_series, atr),
        atr_to_median=(atr / median) if median > 0 else None,
        range_pct=((window_high - window_low) / last_close * 100.0 if last_close > 0 else None),
    )


def compute_momentum(candles: list[Candle], roc_period: int = 10) -> MomentumFeatures | None:
    if len(candles) < roc_period + 1:
        return None
    closes = [c.close for c in candles]
    base = closes[-roc_period - 1]
    roc = (closes[-1] - base) / base * 100.0 if base > 0 else None
    diffs = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    recent = diffs[-roc_period:]
    net_up = (closes[-1] - closes[-roc_period - 1]) > 0
    agreeing = sum(1 for d in recent if d != 0 and (d > 0) is net_up)
    persistence = agreeing / len(recent) if recent else None
    streak = 0
    for d in reversed(diffs):
        if streak > 0 and ((d > 0) != (diffs[len(diffs) - streak] > 0)):
            break
        if d == 0:
            break
        streak += 1
    return MomentumFeatures(roc_pct=roc, persistence=persistence, consecutive_same_dir=streak)


def _swing_points(candles: list[Candle], wing: int = 2) -> tuple[list[int], list[int]]:
    """Fractal swing indices: a swing high/low dominates `wing` bars each side."""
    highs: list[int] = []
    lows: list[int] = []
    n = len(candles)
    for i in range(wing, n - wing):
        window = [c for j, c in enumerate(candles[i - wing : i + wing + 1]) if j != wing]
        if all(candles[i].high >= c.high for c in window):
            highs.append(i)
        if all(candles[i].low <= c.low for c in window):
            lows.append(i)
    return highs, lows


def compute_structure(
    candles: list[Candle], wing: int = 2
) -> tuple[StructureFeatures | None, list[tuple[float, str]]]:
    """Structure score + raw swing levels [(price, kind), ...]."""
    if len(candles) < MIN_FEATURE_BARS:
        return None, []
    highs, lows = _swing_points(candles, wing)
    hh = lh = hl = ll = 0
    for prev, cur in zip(highs, highs[1:], strict=False):
        if candles[cur].high > candles[prev].high:
            hh += 1
        else:
            lh += 1
    for prev, cur in zip(lows, lows[1:], strict=False):
        if candles[cur].low > candles[prev].low:
            hl += 1
        else:
            ll += 1
    if len(highs) >= 2 and len(lows) >= 2:
        last_h_up = candles[highs[-1]].high > candles[highs[-2]].high
        last_l_up = candles[lows[-1]].low > candles[lows[-2]].low
        if last_h_up and last_l_up:
            structure_trend = "up"
        elif not last_h_up and not last_l_up:
            structure_trend = "down"
        else:
            structure_trend = "range"
    else:
        structure_trend = "insufficient"
    features = StructureFeatures(
        swing_highs=len(highs),
        swing_lows=len(lows),
        higher_highs=hh,
        lower_highs=lh,
        higher_lows=hl,
        lower_lows=ll,
        structure_trend=structure_trend,
    )
    levels = [(candles[i].high, "resistance") for i in highs]
    levels += [(candles[i].low, "support") for i in lows]
    return features, levels


def compute_support_resistance(
    levels: list[tuple[float, str]],
    current_price: float,
    atr: float | None,
    *,
    cluster_atr: float = 0.3,
    max_levels: int = 6,
) -> list[SupportResistanceLevel]:
    """Cluster nearby swing points into S/R levels ranked by proximity."""
    if not levels or current_price <= 0:
        return []
    tol = (atr if atr and atr > 0 else current_price * 0.001) * cluster_atr
    clusters: list[dict] = []
    for price, kind in sorted(levels):
        for cluster in clusters:
            if abs(cluster["price"] - price) <= tol:
                total = cluster["price"] * cluster["touches"] + price
                cluster["touches"] += 1
                cluster["price"] = total / cluster["touches"]
                if kind == "resistance":
                    cluster["resistance"] += 1
                else:
                    cluster["support"] += 1
                break
        else:
            clusters.append(
                {
                    "price": price,
                    "touches": 1,
                    "resistance": 1 if kind == "resistance" else 0,
                    "support": 1 if kind == "support" else 0,
                }
            )
    out: list[SupportResistanceLevel] = []
    for cluster in clusters:
        kind = "resistance" if cluster["resistance"] >= cluster["support"] else "support"
        out.append(
            SupportResistanceLevel(
                price=round(cluster["price"], 6),
                kind=kind,
                touches=cluster["touches"],
                distance_pct=(cluster["price"] - current_price) / current_price * 100.0,
            )
        )
    out.sort(key=lambda lvl: abs(lvl.distance_pct))
    return out[:max_levels]


def compute_breakout(
    candles: list[Candle],
    atr: float | None,
    *,
    exclude_last: int = 3,
    threshold_atr: float = 0.1,
    confirm_bars: int = 2,
    max_range_atr: float = 20.0,
    max_range_drift_atr: float = 4.0,
) -> BreakoutState:
    """Detect range-boundary breaks with confirmation / failure tracking.

    A breakout requires a genuine consolidation first: the prior range width
    must be bounded relative to ATR (a sustained trend is not "breaking out"
    of its own trailing range) and the prior segment must not drift more than
    `max_range_drift_atr` ATRs — a range, not a directional move.
    """
    n = len(candles)
    if n < exclude_last + MIN_FEATURE_BARS:
        return BreakoutState(state="none")
    prior = candles[: n - exclude_last]
    hi = max(c.high for c in prior)
    lo = min(c.low for c in prior)
    width = hi - lo
    if width <= 0:
        return BreakoutState(state="none")
    if atr and atr > 0:
        if width > max_range_atr * atr:
            return BreakoutState(state="none")  # too wide to be a range
        drift = abs(prior[-1].close - prior[0].close)
        if drift > max_range_drift_atr * atr:
            return BreakoutState(state="none")  # prior segment is directional
    thr = (atr if atr and atr > 0 else width / 10.0) * threshold_atr
    recent = candles[n - exclude_last :]
    last_close = recent[-1].close

    up_outside = sum(1 for c in recent if c.close > hi + thr)
    down_outside = sum(1 for c in recent if c.close < lo - thr)

    if up_outside > 0:
        extreme = max(c.high for c in recent)
        retrace = (extreme - last_close) / (extreme - hi) * 100.0 if extreme > hi else 0.0
        if last_close <= hi:
            return BreakoutState(
                state="failed",
                direction="up",
                boundary=hi,
                bars_outside=up_outside,
                retrace_pct=retrace,
            )
        state = "confirmed" if up_outside >= confirm_bars else "pending"
        return BreakoutState(
            state=state,
            direction="up",
            boundary=hi,
            bars_outside=up_outside,
            retrace_pct=retrace,
        )
    if down_outside > 0:
        extreme = min(c.low for c in recent)
        retrace = (last_close - extreme) / (lo - extreme) * 100.0 if extreme < lo else 0.0
        if last_close >= lo:
            return BreakoutState(
                state="failed",
                direction="down",
                boundary=lo,
                bars_outside=down_outside,
                retrace_pct=retrace,
            )
        state = "confirmed" if down_outside >= confirm_bars else "pending"
        return BreakoutState(
            state=state,
            direction="down",
            boundary=lo,
            bars_outside=down_outside,
            retrace_pct=retrace,
        )
    return BreakoutState(state="none")
