"""Synthesis: weigh agent disagreement into a structured TradeThesis.

Deterministic. NO_TRADE is the default outcome — a directional thesis must
earn it through participation, alignment, and conviction. Veto-capable
opinions (data quality, news, failed breakouts, regime) overrule everything.
"""

from __future__ import annotations

from mt5_platform.agents.contracts import (
    AgentContext,
    AgentOpinion,
    AgentRole,
    Stance,
    TradeThesis,
)
from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, OrderSide, RegimeLabel, Severity
from mt5_platform.common.events import AuditEvent

DEFAULT_WEIGHTS: dict[AgentRole, float] = {
    AgentRole.MARKET_INTELLIGENCE: 0.7,
    AgentRole.MARKET_REGIME: 1.0,
    AgentRole.STRUCTURE: 1.0,
    AgentRole.MOMENTUM: 0.9,
    AgentRole.BREAKOUT: 1.0,
    AgentRole.MEAN_REVERSION: 0.6,
    AgentRole.HISTORICAL: 0.9,
    AgentRole.NEWS: 1.0,
    AgentRole.STRATEGY_EVALUATION: 0.8,
}


class SynthesisAgent:
    """Debate synthesizer. Consumes opinions, produces one TradeThesis."""

    name = "synthesis"

    def __init__(
        self,
        *,
        veto_confidence: float = 0.8,
        min_participation: float = 0.8,
        min_conviction: float = 1.0,
        min_alignment: float = 0.6,
        min_net_ratio: float = 0.6,
        atr_stop_mult: float = 1.5,
        atr_target_mult: float = 3.0,
    ) -> None:
        self.veto_confidence = veto_confidence
        self.min_participation = min_participation
        self.min_conviction = min_conviction
        self.min_alignment = min_alignment
        self.min_net_ratio = min_net_ratio
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult

    def synthesize(self, ctx: AgentContext, opinions: list[AgentOpinion]) -> TradeThesis:
        mctx = ctx.market_context
        reasons: list[str] = []
        invalidation: list[str] = [
            "price closes beyond the stop-loss level",
            f"regime departs from {mctx.regime.value if mctx.regime else 'current'}",
            "data quality degrades to CRITICAL",
            "spread reaches extreme level",
        ]

        def no_trade(reason: str) -> TradeThesis:
            reasons.append(reason)
            thesis = TradeThesis(
                context_id=mctx.context_id,
                instrument=mctx.instrument,
                action=Stance.NO_TRADE,
                regime=mctx.regime.value if mctx.regime else None,
                volatility_state=(
                    f"ATR/median {mctx.volatility.atr_to_median:.2f}"
                    if mctx.volatility and mctx.volatility.atr_to_median is not None
                    else None
                ),
                total_agents=len(opinions),
                opinions=opinions,
                reasons=reasons,
                invalidation=invalidation,
            )
            audit_log.emit(
                AuditEvent(
                    component="synthesis",
                    event_type=AuditEventType.THESIS_REJECTED.value,
                    severity=Severity.INFO,
                    symbol=mctx.instrument,
                    payload={"context_id": mctx.context_id, "reason": reason},
                )
            )
            return thesis

        # --- Hard gates ---------------------------------------------------
        if not mctx.usable_for_trading:
            return no_trade(
                f"market context not usable for trading "
                f"(quality={mctx.data_quality.level.value}, "
                f"regime={mctx.regime.value if mctx.regime else None})"
            )
        if mctx.regime in {RegimeLabel.UNDEFINED, RegimeLabel.ABNORMAL}:
            return no_trade(f"regime is {mctx.regime.value} — refusing to act")
        vetoes = [
            o
            for o in opinions
            if o.stance is Stance.NO_TRADE and o.confidence >= self.veto_confidence
        ]
        if vetoes:
            return no_trade(
                "vetoed by: " + "; ".join(f"{o.agent_name} ({o.rationale})" for o in vetoes)
            )

        # --- Weighted directional debate ----------------------------------
        def role_weight(o: AgentOpinion) -> float:
            return DEFAULT_WEIGHTS.get(o.role, 0.7)

        buy_w = sum(o.confidence * role_weight(o) for o in opinions if o.stance is Stance.BUY)
        sell_w = sum(o.confidence * role_weight(o) for o in opinions if o.stance is Stance.SELL)
        total_directional = buy_w + sell_w
        cautions = [o for o in opinions if o.stance is Stance.CAUTION]
        if total_directional < self.min_participation:
            return no_trade(
                f"insufficient directional evidence (participation "
                f"{total_directional:.2f} < {self.min_participation})"
            )
        net = buy_w - sell_w
        direction = OrderSide.BUY if net > 0 else OrderSide.SELL
        aligned = [
            o
            for o in opinions
            if (OrderSide.BUY if o.stance is Stance.BUY else OrderSide.SELL) is direction
        ]
        opposed = [
            o
            for o in opinions
            if o.stance in {Stance.BUY, Stance.SELL}
            and (OrderSide.BUY if o.stance is Stance.BUY else OrderSide.SELL) is not direction
        ]
        # Confidence-weighted alignment (count-based alignment hid dissent).
        aligned_w = sum(o.confidence * role_weight(o) for o in aligned)
        opposed_w = sum(o.confidence * role_weight(o) for o in opposed)
        caution_w = sum(o.confidence * role_weight(o) for o in cautions)
        total_w = aligned_w + opposed_w + caution_w
        alignment_ratio = aligned_w / max(total_w, 1e-9)
        # Net majority: how much of the directional weight agrees with the
        # chosen direction. Prevents "6-vs-2 automatically means BUY".
        net_ratio = abs(net) / max(total_directional, 1e-9)
        conviction = abs(net) * alignment_ratio
        evidence_summary = {
            "buy_weight": round(buy_w, 3),
            "sell_weight": round(sell_w, 3),
            "net": round(net, 3),
            "net_ratio": round(net_ratio, 3),
            "alignment_ratio": round(alignment_ratio, 3),
            "conviction": round(conviction, 3),
            "aligned": [o.agent_name for o in aligned],
            "opposed": [o.agent_name for o in opposed],
            "cautions": [o.agent_name for o in cautions],
        }
        if net_ratio < self.min_net_ratio:
            return no_trade(
                f"net majority {net_ratio:.0%} below {self.min_net_ratio:.0%} "
                f"— minority dissent blocks thesis ({evidence_summary})"
            )
        if alignment_ratio < self.min_alignment:
            return no_trade(
                f"agent alignment {alignment_ratio:.2f} below {self.min_alignment} "
                f"— evidence conflicts ({evidence_summary})"
            )
        if conviction < self.min_conviction:
            return no_trade(
                f"conviction {conviction:.2f} below {self.min_conviction} ({evidence_summary})"
            )

        # --- Build directional thesis ---------------------------------------
        atr = mctx.volatility.atr if mctx.volatility else None
        price = mctx.current_price
        direction_stance = Stance.BUY if direction is OrderSide.BUY else Stance.SELL
        entry = price
        stop = None
        target = None
        if atr and atr > 0 and price is not None:
            if direction is OrderSide.BUY:
                stop = price - atr * self.atr_stop_mult
                target = price + atr * self.atr_target_mult
            else:
                stop = price + atr * self.atr_stop_mult
                target = price - atr * self.atr_target_mult
        thesis = TradeThesis(
            context_id=mctx.context_id,
            instrument=mctx.instrument,
            action=direction_stance,
            direction=direction,
            regime=mctx.regime.value if mctx.regime else None,
            confidence=round(min(conviction, 1.0), 4),
            aligned_agents=len(aligned),
            opposed_agents=len(opposed),
            caution_agents=len(cautions),
            neutral_agents=sum(1 for o in opinions if o.stance is Stance.NEUTRAL),
            total_agents=len(opinions),
            historical_expectancy=next(
                (o.evidence.get("expectancy") for o in opinions if o.role == AgentRole.HISTORICAL),
                None,
            ),
            volatility_state=(
                f"ATR/median {mctx.volatility.atr_to_median:.2f}"
                if mctx.volatility and mctx.volatility.atr_to_median is not None
                else None
            ),
            news_risk=next(
                (
                    "high"
                    if o.stance is Stance.NO_TRADE
                    else "medium"
                    if o.stance is Stance.CAUTION
                    else "low"
                    for o in opinions
                    if o.role == AgentRole.NEWS
                ),
                None,
            ),
            entry=round(entry, 4) if entry is not None else None,
            stop_loss=round(stop, 4) if stop is not None else None,
            take_profit=round(target, 4) if target is not None else None,
            invalidation=invalidation,
            reasons=reasons
            + [
                f"weighted {direction.value} conviction {conviction:.2f} "
                f"with {alignment_ratio:.0%} agent alignment"
            ],
            opinions=opinions,
        )
        audit_log.emit(
            AuditEvent(
                component="synthesis",
                event_type=AuditEventType.THESIS_EMITTED.value,
                severity=Severity.INFO,
                symbol=mctx.instrument,
                payload={
                    "thesis_id": thesis.thesis_id,
                    "context_id": mctx.context_id,
                    "direction": direction.value,
                    "confidence": thesis.confidence,
                    "aligned": len(aligned),
                    "opposed": len(opposed),
                },
            )
        )
        return thesis
