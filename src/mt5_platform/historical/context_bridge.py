"""Build SetupFeatures snapshots from a canonical MarketContext.

The evidence engine needs a frozen, comparable fingerprint of the market
at decision time. This function produces one from a MarketContext, with
deterministic fallback values where features are absent.

This module is the only place where the live MarketContext is converted
into the frozen SetupFeatures used by the evidence engine. The
conversion is deliberately one-way: SetupFeatures must never reach
back into the live context.
"""

from __future__ import annotations

from mt5_platform.context import MarketContext
from mt5_platform.historical.models import SetupFeatures


def setup_from_context(
    ctx: MarketContext,
    *,
    timeframe: str = "M5",
) -> SetupFeatures:
    """Snapshot a MarketContext into a SetupFeatures for evidence queries.

    Range position is computed from the primary candle window: where the
    current price sits between the low and high of the primary series.
    A position of 0 means at the low, 1 means at the high.
    """
    candles = ctx.candles.get(timeframe) or []
    range_position: float | None = None
    if candles:
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        lo, hi = min(lows), max(highs)
        span = hi - lo
        if span > 0 and ctx.current_price > 0:
            range_position = (ctx.current_price - lo) / span
            range_position = max(0.0, min(1.0, range_position))

    return SetupFeatures(
        instrument=ctx.instrument,
        timestamp=ctx.timestamp,
        regime=ctx.regime,
        session=ctx.session.label,
        timeframe=timeframe,
        trend_slope_pct=ctx.trend.slope_per_bar_pct if ctx.trend else None,
        trend_efficiency=ctx.trend.efficiency_ratio if ctx.trend else None,
        volatility_atr=ctx.volatility.atr if ctx.volatility else None,
        volatility_atr_to_median=(ctx.volatility.atr_to_median if ctx.volatility else None),
        momentum_roc_pct=ctx.momentum.roc_pct if ctx.momentum else None,
        momentum_persistence=ctx.momentum.persistence if ctx.momentum else None,
        structure_trend=(ctx.structure.structure_trend if ctx.structure else "insufficient"),
        range_position=range_position,
        breakout_state=ctx.breakout.state,
        data_quality=ctx.data_quality.level.value,
    )
