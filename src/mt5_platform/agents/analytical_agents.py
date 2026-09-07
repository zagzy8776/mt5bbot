"""Deterministic analytical agents over the canonical MarketContext.

Each agent answers one question with measurable evidence. They are free to
disagree — that is the point.
"""

from __future__ import annotations

from mt5_platform.agents.contracts import Agent, AgentContext, AgentOpinion, Stance
from mt5_platform.common.enums import DataQualityLevel, OrderSide, RegimeLabel
from mt5_platform.context import Candle


def _primary_candles(ctx: AgentContext) -> list[Candle]:
    """Primary timeframe candles: the longest available series (>= 25 bars)."""
    mctx = ctx.market_context
    if not mctx.candles:
        return []
    best = max(mctx.candles.items(), key=lambda kv: len(kv[1]))
    return best[1] if len(best[1]) >= 25 else []


def _sma(candles: list[Candle], period: int) -> float | None:
    if len(candles) < period:
        return None
    tail = candles[-period:]
    return sum(c.close for c in tail) / period


def _structure_share(structure) -> float:
    """|net structure| normalized to [0, 1] from swing counts."""
    up = structure.higher_highs + structure.higher_lows
    down = structure.lower_highs + structure.lower_lows
    total = max(up + down, 1)
    return abs(up - down) / total


class MarketIntelligenceAgent(Agent):
    """'What is happening right now?' — trend read, session, spread, quality."""

    name = "market_intelligence"
    role = AgentRole.MARKET_INTELLIGENCE
    weight = 0.7

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        mctx = ctx.market_context
        ev: dict = {
            "session": mctx.session.label,
            "data_quality": mctx.data_quality.level.value,
            "liquidity": mctx.liquidity.level,
        }
        if mctx.data_quality.level is DataQualityLevel.CRITICAL:
            return self._opinion(
                ctx, stance=Stance.NO_TRADE, confidence=0.9,
                evidence=ev, rationale="market data is critically degraded",
            )
        trend = mctx.trend
        if trend is None or trend.slope_per_bar_pct is None:
            ev["reason"] = "insufficient_history"
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3, evidence=ev,
                rationale="not enough data to characterize the market",
            )
        ev["slope_per_bar_pct"] = round(trend.slope_per_bar_pct, 6)
        ev["net_change_pct"] = round(trend.net_change_pct or 0.0, 4)
        direction = OrderSide.BUY if trend.slope_per_bar_pct > 0 else OrderSide.SELL
        stance = Stance.BUY if direction is OrderSide.BUY else Stance.SELL
        conf = 0.4 + min(abs(trend.slope_per_bar_pct) / 0.05, 1.0) * 0.15
        if mctx.data_quality.level is DataQualityLevel.DEGRADED:
            conf *= 0.6
            ev["confidence_discount"] = "degraded_data"
        return self._opinion(
            ctx, stance=stance, confidence=conf, direction=direction, evidence=ev,
            rationale=f"price drifting {direction.value} "
            f"(slope {trend.slope_per_bar_pct:.5f}%/bar)",
        )


class RegimeAgent(Agent):
    """'What type of market are we in?' — echoes the regime layer with weight."""

    name = "market_regime"
    role = AgentRole.MARKET_REGIME
    weight = 1.0

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        mctx = ctx.market_context
        ev: dict = {
            "regime": mctx.regime.value if mctx.regime else None,
            "regime_confidence": mctx.regime_confidence,
            "reason": mctx.regime_evidence.get("reason"),
        }
        regime = mctx.regime
        if regime is None or regime in {RegimeLabel.UNDEFINED, RegimeLabel.ABNORMAL}:
            label = regime.value if regime else "missing"
            return self._opinion(
                ctx, stance=Stance.NO_TRADE, confidence=0.9, evidence=ev,
                rationale=f"regime is {label} — not tradable",
            )
        if regime is RegimeLabel.RANGING:
            return self._opinion(
                ctx, stance=Stance.CAUTION, confidence=0.6, evidence=ev,
                rationale="ranging market: trend-following entries are historically weak",
            )
        if regime is RegimeLabel.BREAKOUT:
            direction = (
                OrderSide.BUY if mctx.breakout.direction == "up" else OrderSide.SELL
            )
            return self._opinion(
                ctx,
                stance=Stance.BUY if direction is OrderSide.BUY else Stance.SELL,
                confidence=mctx.regime_confidence or 0.6, direction=direction,
                evidence=ev,
                rationale=f"confirmed {mctx.breakout.direction} breakout of prior range",
            )
        trend = mctx.trend
        if regime is RegimeLabel.TRENDING and trend is not None:
            direction = (
                OrderSide.BUY if (trend.slope_per_bar_pct or 0) > 0 else OrderSide.SELL
            )
            return self._opinion(
                ctx,
                stance=Stance.BUY if direction is OrderSide.BUY else Stance.SELL,
                confidence=(mctx.regime_confidence or 0.5) * 0.9, direction=direction,
                evidence=ev,
                rationale=f"trending market ({mctx.regime_evidence.get('reason')})",
            )
        return self._opinion(
            ctx, stance=Stance.CAUTION,
            confidence=mctx.regime_confidence or 0.5, evidence=ev,
            rationale=f"regime {regime.value}: wait for clearer conditions",
        )


class StructureAgent(Agent):
    """'What does price structure say?' — swing structure and S/R positioning."""

    name = "structure"
    role = AgentRole.STRUCTURE
    weight = 1.0

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        structure = ctx.market_context.structure
        ev: dict = {}
        if structure is None:
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3, evidence=ev,
                rationale="insufficient data for structure analysis",
            )
        ev["structure_trend"] = structure.structure_trend
        ev["higher_highs"] = structure.higher_highs
        ev["lower_lows"] = structure.lower_lows
        if ctx.market_context.support_resistance:
            nearest = ctx.market_context.support_resistance[0]
            ev["nearest_level"] = {
                "price": nearest.price,
                "kind": nearest.kind,
                "distance_pct": round(nearest.distance_pct, 4),
            }
        if structure.structure_trend == "up":
            return self._opinion(
                ctx, stance=Stance.BUY,
                confidence=0.55 + 0.3 * _structure_share(structure),
                direction=OrderSide.BUY, evidence=ev,
                rationale="higher highs and higher lows intact",
            )
        if structure.structure_trend == "down":
            return self._opinion(
                ctx, stance=Stance.SELL,
                confidence=0.55 + 0.3 * _structure_share(structure),
                direction=OrderSide.SELL, evidence=ev,
                rationale="lower highs and lower lows intact",
            )
        return self._opinion(
            ctx, stance=Stance.CAUTION, confidence=0.5, evidence=ev,
            rationale=f"structure is {structure.structure_trend}",
        )


class MomentumAgent(Agent):
    """'Is directional pressure actually present?' — ATR-normalized ROC."""

    name = "momentum"
    role = AgentRole.MOMENTUM
    weight = 0.9

    def __init__(self, *, roc_atr_buy: float = 0.5, persistence_min: float = 0.6):
        self.roc_atr_buy = roc_atr_buy
        self.persistence_min = persistence_min

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        mctx = ctx.market_context
        ev: dict = {}
        if (
            mctx.momentum is None
            or mctx.volatility is None
            or mctx.volatility.atr is None
        ):
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3, evidence=ev,
                rationale="insufficient data for momentum analysis",
            )
        roc_pct = mctx.momentum.roc_pct or 0.0
        atr = mctx.volatility.atr
        price = mctx.current_price
        roc_atr = (roc_pct / 100.0 * price) / atr if atr > 0 and price > 0 else 0.0
        ev["roc_pct"] = round(roc_pct, 4)
        ev["roc_atr"] = round(roc_atr, 3)
        ev["persistence"] = mctx.momentum.persistence
        persist = mctx.momentum.persistence or 0.0
        if roc_atr >= self.roc_atr_buy and persist >= self.persistence_min:
            return self._opinion(
                ctx, stance=Stance.BUY,
                confidence=0.55 + min(roc_atr, 2.0) * 0.15,
                direction=OrderSide.BUY, evidence=ev,
                rationale=f"upward pressure present ({roc_atr:.2f} ATR per 10 bars)",
            )
        if roc_atr <= -self.roc_atr_buy and persist >= self.persistence_min:
            return self._opinion(
                ctx, stance=Stance.SELL,
                confidence=0.55 + min(abs(roc_atr), 2.0) * 0.15,
                direction=OrderSide.SELL, evidence=ev,
                rationale=f"downward pressure present ({roc_atr:.2f} ATR per 10 bars)",
            )
        return self._opinion(
            ctx, stance=Stance.NEUTRAL, confidence=0.4, evidence=ev,
            rationale="no decisive directional pressure",
        )


class BreakoutAgent(Agent):
    """'Is there a genuine breakout or a likely failure?'"""

    name = "breakout"
    role = AgentRole.BREAKOUT
    weight = 1.0

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        bo = ctx.market_context.breakout
        ev = {"breakout_state": bo.state, "direction": bo.direction,
              "boundary": bo.boundary, "bars_outside": bo.bars_outside,
              "retrace_pct": bo.retrace_pct}
        if bo.state == "confirmed":
            direction = OrderSide.BUY if bo.direction == "up" else OrderSide.SELL
            conf = 0.7 - min((bo.retrace_pct or 0.0) / 100.0, 0.2)
            return self._opinion(
                ctx,
                stance=Stance.BUY if direction is OrderSide.BUY else Stance.SELL,
                confidence=conf, direction=direction, evidence=ev,
                rationale=f"confirmed {bo.direction} breakout "
                f"({bo.bars_outside} closes beyond prior boundary)",
            )
        if bo.state == "failed":
            return self._opinion(
                ctx, stance=Stance.NO_TRADE, confidence=0.8, evidence=ev,
                rationale=f"{bo.direction} breakout pierced the boundary then reversed "
                f"({bo.retrace_pct:.0f}% retrace) — likely failure",
            )
        if bo.state == "pending":
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.5, evidence=ev,
                rationale="boundary tested but breakout unconfirmed",
            )
        return self._opinion(
            ctx, stance=Stance.NEUTRAL, confidence=0.4, evidence=ev,
            rationale="no breakout conditions present",
        )


class MeanReversionAgent(Agent):
    """'Is price stretched and likely to revert?' — the designed dissenter.

    Opposes extended moves in ATR units relative to the 20-bar mean.
    """

    name = "mean_reversion"
    role = AgentRole.MEAN_REVERSION
    weight = 0.6

    def __init__(self, *, stretch_atr: float = 1.5, sma_period: int = 20):
        self.stretch_atr = stretch_atr
        self.sma_period = sma_period

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        mctx = ctx.market_context
        ev: dict = {}
        if mctx.volatility is None or mctx.volatility.atr is None:
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3, evidence=ev,
                rationale="insufficient data for stretch analysis",
            )
        atr = mctx.volatility.atr
        if atr <= 0:
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3, evidence=ev,
                rationale="ATR unavailable",
            )
        sma = _sma(_primary_candles(ctx), self.sma_period)
        if sma is None or sma <= 0:
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3, evidence=ev,
                rationale="insufficient candles for mean calculation",
            )
        stretch = (mctx.current_price - sma) / atr
        ev["sma20"] = round(sma, 4)
        ev["stretch_atr"] = round(stretch, 3)
        if stretch >= self.stretch_atr:
            return self._opinion(
                ctx, stance=Stance.SELL,
                confidence=min(0.85, 0.5 + (stretch - self.stretch_atr) * 0.15),
                direction=OrderSide.SELL, evidence=ev,
                rationale=f"price {stretch:.2f} ATRs above 20-bar mean — statistically "
                "stretched, reversion risk",
            )
        if stretch <= -self.stretch_atr:
            return self._opinion(
                ctx, stance=Stance.BUY,
                confidence=min(0.85, 0.5 + (abs(stretch) - self.stretch_atr) * 0.15),
                direction=OrderSide.BUY, evidence=ev,
                rationale=f"price {abs(stretch):.2f} ATRs below 20-bar mean — "
                "statistically stretched, reversion risk",
            )
        return self._opinion(
            ctx, stance=Stance.NEUTRAL, confidence=0.4, evidence=ev,
            rationale=f"price within normal range of mean ({stretch:.2f} ATR)",
        )