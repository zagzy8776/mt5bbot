"""Support agents: news risk, strategy-regime fit, historical evidence (v1).

The Historical agent is deliberately honest: without measured evidence it says
so. It will consume the Phase C evidence engine; it never invents statistics.
"""

from __future__ import annotations

from mt5_platform.agents.contracts import Agent, AgentContext, AgentOpinion, AgentRole, Stance
from mt5_platform.common.enums import OrderSide, RegimeLabel

# Which regimes each registered strategy is structurally suited for. This is a
# prior, NOT a profitability claim — measured performance arrives in Phase C.
STRATEGY_REGIME_FIT: dict[str, set[RegimeLabel]] = {
    "sma_crossover": {RegimeLabel.TRENDING},
    "breakout": {RegimeLabel.BREAKOUT},
    "momentum": {RegimeLabel.TRENDING, RegimeLabel.BREAKOUT},
    "mean_reversion": {RegimeLabel.RANGING, RegimeLabel.LOW_VOLATILITY},
}


class NewsAgent(Agent):
    """'Are external events creating additional risk?' — calendar/manual feed v1."""

    name = "news"
    role = AgentRole.NEWS
    weight = 1.0

    def __init__(
        self,
        *,
        high_impact_minutes: float = 30.0,
        medium_impact_minutes: float = 15.0,
    ):
        self.high_impact_minutes = high_impact_minutes
        self.medium_impact_minutes = medium_impact_minutes

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        relevant: list[dict] = []
        for event in ctx.news_events:
            impact = str(event.get("impact", "low")).lower()
            within = float(event.get("within_minutes", 1e9))
            if (
                impact == "high" and within <= self.high_impact_minutes
                or impact == "medium" and within <= self.medium_impact_minutes
            ):
                relevant.append(event)
        ev = {"relevant_events": relevant, "feed_size": len(ctx.news_events)}
        if any(str(e.get("impact", "")).lower() == "high" for e in relevant):
            return self._opinion(
                ctx, stance=Stance.NO_TRADE, confidence=0.9, evidence=ev,
                rationale="high-impact event imminent — standing aside",
            )
        if relevant:
            return self._opinion(
                ctx, stance=Stance.CAUTION, confidence=0.6, evidence=ev,
                rationale="medium-impact event near — reduced appetite",
            )
        return self._opinion(
            ctx, stance=Stance.NEUTRAL, confidence=0.5, evidence=ev,
            rationale="no relevant events on the feed",
        )


class StrategyEvaluationAgent(Agent):
    """'Which strategy is appropriate for the current conditions?'

    A candidate signal from a strategy that does not fit the current regime is
    reported as CAUTION — the exact 'valid signal, wrong regime' reasoning the
    system must exhibit.
    """

    name = "strategy_evaluation"
    role = AgentRole.STRATEGY_EVALUATION
    weight = 0.8

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        mctx = ctx.market_context
        ev: dict = {
            "regime": mctx.regime.value if mctx.regime else None,
            "candidates": len(ctx.candidate_signals),
        }
        if not ctx.candidate_signals:
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.4, evidence=ev,
                rationale="no candidate signals to evaluate",
            )
        if mctx.regime is None or mctx.regime is RegimeLabel.UNDEFINED:
            return self._opinion(
                ctx, stance=Stance.CAUTION, confidence=0.7, evidence=ev,
                rationale="cannot judge strategy fit without a regime",
            )
        first = ctx.candidate_signals[0]
        fitted = STRATEGY_REGIME_FIT.get(first.strategy_name, set())
        ev["strategy"] = first.strategy_name
        ev["regime_fit"] = sorted(r.value for r in fitted)
        if mctx.regime in fitted:
            direction = (
                OrderSide.BUY if first.direction is OrderSide.BUY else OrderSide.SELL
            )
            return self._opinion(
                ctx,
                stance=Stance.BUY if direction is OrderSide.BUY else Stance.SELL,
                confidence=0.5 + first.confidence * 0.3, direction=direction,
                evidence=ev,
                rationale=f"{first.strategy_name} signal fits the "
                f"{mctx.regime.value} regime",
            )
        return self._opinion(
            ctx, stance=Stance.CAUTION, confidence=0.7, evidence=ev,
            rationale=f"{first.strategy_name} produced a signal but the "
            f"{mctx.regime.value} regime historically does not suit it",
        )


class HistoricalAgent(Agent):
    """'What happened in comparable historical situations?' — v1 scaffold.

    Until the Phase C evidence engine exists this agent reports honestly that
    there is no measured evidence. It never fabricates statistics.
    """

    name = "historical"
    role = AgentRole.HISTORICAL
    weight = 0.9

    def evaluate(self, ctx: AgentContext) -> AgentOpinion:
        evidence = ctx.historical_evidence
        if not evidence:
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3,
                evidence={"status": "no_measured_evidence"},
                rationale="no historical evidence available yet "
                "(evidence engine arrives in Phase C)",
            )
        expectancy = evidence.get("expectancy")
        sample = int(evidence.get("sample_size", 0))
        min_sample = int(evidence.get("min_sample", 30))
        ev = {"status": "measured", "expectancy": expectancy, "sample_size": sample}
        if expectancy is None or sample < min_sample:
            return self._opinion(
                ctx, stance=Stance.NEUTRAL, confidence=0.3, evidence=ev,
                rationale="historical sample too small to support the decision",
            )
        stance = Stance.CAUTION if float(expectancy) <= 0 else Stance.NEUTRAL
        return self._opinion(
            ctx, stance=stance, confidence=0.6, evidence=ev,
            rationale=f"comparable setups show expectancy {expectancy} "
            f"over {sample} samples",
        )